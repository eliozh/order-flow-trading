"""``oft daemon start|stop|status|logs``: supervisor-facing daemon control."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.jobs import (
    POSITION_MANAGE_JOB_TYPE,
    POSITION_MONITOR_JOB_TYPE,
    TRIGGER_JOB_TYPE,
    JobLedger,
    ManagementJobOutcome,
    ManagementJobRunner,
    PositionMonitorRunner,
    ScheduledScanRunner,
    TriggerJobRunner,
    latest_jobs_payload,
)
from oft.application.scheduler import (
    ManagementDaemonScheduler,
    PositionMonitorDaemonScheduler,
    ScanDaemonScheduler,
    ScheduleState,
    SettingsPeriodSync,
    SettingsSyncDaemonScheduler,
    TriggerDaemonScheduler,
    align_to_boundary,
)
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, Settings, load_settings
from oft.domain.time import utc_timestamp

_LOGGER = structlog.get_logger(__name__)

daemon_app = typer.Typer(
    help="Daemon lifecycle (local API/UI, live ingestion, and the scan scheduler).",
    no_args_is_help=True,
    add_completion=False,
)


def pid_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "daemon.pid"


def log_path(settings: Settings) -> Path:
    return settings.data_dir / "logs" / "daemon.log"


def read_pid(settings: Settings) -> int | None:
    path = pid_path(settings)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def pid_meta_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "daemon.pid.json"


def pid_matches(settings: Settings, pid: int) -> bool:
    """Verify the PID still names the daemon we started (start-time fence)."""
    if not process_alive(pid):
        return False
    try:
        import json

        meta = json.loads(pid_meta_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    expected = meta.get("start_time")
    current = _process_start_time(pid)
    return expected is None or current is None or expected == current


def write_pid(settings: Settings) -> None:
    path = pid_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid = os.getpid()
    path.write_text(str(pid), encoding="utf-8")
    path.chmod(0o600)
    import json

    meta = pid_meta_path(settings)
    meta.write_text(
        json.dumps({"pid": pid, "start_time": _process_start_time(pid)}),
        encoding="utf-8",
    )
    meta.chmod(0o600)


def _settings_or_exit(json_output: bool) -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def schedule_state_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "schedule.json"


def _schedule_state(
    settings: Settings | None = None,
    *,
    container: Any | None = None,
    now: datetime | None = None,
    json_output: bool = False,
) -> ScheduleState:
    """The persisted ``schedule.json`` state with the saved period overlaid.

    The persisted row is resolved before the environment period is validated
    (M3) and a due pending change is promoted before the value is used (M1), so
    ``daemon status`` / ``scan status`` never show a change that already applied
    or fail because ``OFT_SCAN_PERIOD`` is stale. A corrupt persisted row, an
    invalid environment period, or an invalid jitter is surfaced as
    ``CONFIG_INVALID`` (exit 5) instead of a traceback.

    ``json_output`` keeps a config error in the standard JSON envelope for
    ``daemon status --json`` / ``scan status --json``.
    """
    try:
        if settings is None:
            from oft.application.scheduler import resolve_schedule_settings

            config = resolve_schedule_settings()
            return ScheduleState(period=config.period, jitter_seconds_max=config.jitter_seconds)
        resolved = container
        if resolved is None:
            from oft.bootstrap import build_container

            resolved = build_container(settings)
        from oft.application.settings_service import resolve_schedule_state_overlay

        return resolve_schedule_state_overlay(
            settings=settings,
            store=resolved.store,
            clock=resolved.clock,
            now=now,
        )
    except (ConfigError, ValueError) as exc:
        message = exc.message if isinstance(exc, ConfigError) else str(exc)
        emit_error("CONFIG_INVALID", message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _last_scheduled_run(settings: Settings, *, container: Any | None = None) -> Any:
    from oft.bootstrap import build_container

    resolved = container or build_container(settings)
    for run in resolved.store.list_job_runs("scan"):
        if run.trigger_source == "SCHEDULED":
            return run
    return None


def _latest_job_payloads(settings: Settings, *, container: Any | None = None) -> dict[str, Any]:
    """The aggregated latest-window status of the slice-26 jobs."""
    from oft.bootstrap import build_container

    resolved = container or build_container(settings)
    return latest_jobs_payload(resolved.store)


def _status_payload(
    settings: Settings, state: ScheduleState, *, container: Any | None = None
) -> dict[str, Any]:
    from oft.clock import SystemClock

    pid = read_pid(settings)
    running = pid is not None and process_alive(pid)
    now = SystemClock().now()
    last = _last_scheduled_run(settings, container=container)
    schedule = state.next_run_payload(now)
    latest_jobs = _latest_job_payloads(settings, container=container)
    return {
        "state": "RUNNING" if running else "OFFLINE",
        "pid": pid if running else None,
        "timezone": schedule["timezone"],
        "period": schedule["period"],
        "pending_period": schedule["pending_period"],
        "next_run_at": schedule["next_run_at"],
        "last_run": (
            {
                "run_id": last.run_id,
                "status": last.status,
                "window_start": last.window_start,
                "finished_at": last.finished_at or "",
            }
            if last is not None
            else None
        ),
        "jobs": latest_jobs,
        "generated_at": utc_timestamp(now),
    }


@daemon_app.command("start")
def daemon_start(
    host: Annotated[str | None, typer.Option("--host", help="Loopback bind address.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="API port.")] = None,
    allow_non_loopback: Annotated[
        bool,
        typer.Option(
            "--allow-non-loopback",
            help="Permit a non-loopback bind (unsupported for shared use).",
        ),
    ] = False,
    replay_file: Annotated[
        Path | None,
        typer.Option("--replay-file", help="Replay a recorded WS JSONL fixture before serving."),
    ] = None,
    symbols: Annotated[
        str | None,
        typer.Option(
            "--symbols",
            help="Comma-separated symbols for live ingestion; defaults to starred symbols.",
        ),
    ] = None,
) -> None:
    """Start the daemon, live ingestion, and the UTC scan and trigger schedulers."""
    # Imported lazily: ``main`` imports this module to register the command tree.
    from oft.cli.main import _resolve_serve_settings, _run_live_ingestion, _run_uvicorn

    settings = _resolve_serve_settings(host, port, allow_non_loopback)
    pid = read_pid(settings)
    if pid is not None and pid_matches(settings, pid):
        emit_error(
            "DAEMON_ALREADY_RUNNING",
            f"daemon already running with pid {pid}",
            json_output=False,
        )
        raise typer.Exit(code=1)
    from oft.bootstrap import build_container

    container = build_container(settings, initialize=True)
    # Positional call only: tests replace this seam with a one-argument lambda.
    state = _schedule_state(settings)
    _run_live_ingestion(settings, replay_file, symbols, container=container)
    scheduler = _start_scheduler(settings, state, container=container)
    settings_sync = _start_settings_sync_scheduler(container=container, scan_scheduler=scheduler)
    trigger_scheduler = _start_trigger_scheduler(settings, container=container)
    monitor_scheduler = _start_position_monitor_scheduler(settings, container=container)
    management_scheduler = _start_management_scheduler(
        settings, state, container=container, scan_scheduler=scheduler
    )
    try:
        _run_uvicorn(settings)
    finally:
        management_scheduler.shutdown()
        monitor_scheduler.shutdown()
        trigger_scheduler.shutdown()
        settings_sync.shutdown()
        scheduler.shutdown()
        pid_path(settings).unlink(missing_ok=True)


def _start_scheduler(
    settings: Settings,
    state: ScheduleState,
    *,
    container: Any | None = None,
) -> ScanDaemonScheduler:
    from oft.bootstrap import build_container

    resolved = container or build_container(settings, initialize=True)
    runner = ScheduledScanRunner(container=resolved)
    holder: dict[str, ScanDaemonScheduler] = {}

    def _run_due(window: datetime) -> Any:
        scheduler = holder["scheduler"]
        period = scheduler.state.period_at(resolved.clock.now())
        return runner.run_window(window, period=period)

    ledger = JobLedger(store=resolved.store, clock=resolved.clock)

    def _record_missed(window: datetime) -> None:
        ledger.record_missed(
            "scan",
            utc_timestamp(window),
            reason="scheduled fire missed its boundary",
        )

    scheduler = ScanDaemonScheduler(
        clock=resolved.clock,
        state=state,
        job=_run_due,
        state_path=schedule_state_path(settings),
        on_missed=_record_missed,
    )
    holder["scheduler"] = scheduler
    write_pid(settings)
    if settings.env != "test":
        _catch_up_on_start(resolved, state)
    scheduler.start()
    return scheduler


def _run_settings_sync(sync: Any) -> bool:
    """Run one settings sync tick without letting a config error kill it.

    A corrupt persisted row (``ConfigError``) or an incompatible jitter/period
    (``ValueError``) must never stop the interval scheduler; the API and CLI
    surface ``CONFIG_INVALID`` directly. Returns whether the schedule changed.
    """
    try:
        return bool(sync.sync())
    except (ConfigError, ValueError) as exc:
        _LOGGER.warning("scan_period_sync_failed", error=str(exc))
        return False


def _start_settings_sync_scheduler(
    *,
    container: Any,
    scan_scheduler: ScanDaemonScheduler,
) -> SettingsSyncDaemonScheduler:
    """Apply a saved ``scan.period`` at the next boundary without a restart.

    Every ~15 s the persisted value is compared with the active/pending period;
    only a real difference requests a change, so the running window is never
    rewritten by the sync itself.
    """
    from oft.application.settings_service import SettingsService

    service = SettingsService(store=container.store, clock=container.clock)
    sync = SettingsPeriodSync(
        scheduler=scan_scheduler,
        period_provider=service.persisted_scan_period,
    )

    scheduler = SettingsSyncDaemonScheduler(job=lambda: _run_settings_sync(sync))
    scheduler.start()
    return scheduler


def _start_trigger_scheduler(
    settings: Settings,
    *,
    container: Any,
) -> TriggerDaemonScheduler:
    """Start the separate interval scheduler for trigger evaluation.

    The scan and trigger cadences are independent (ADR-0007). The runner claims
    only the current interval window in the durable ledger, so an overlapping
    tick or a restart never replays missed windows.
    """
    from oft.application.trigger_service import TriggerService

    # A killed process can leave a trigger tick RUNNING forever; the runner
    # only ever claims the current window, so repair expired leases at start.
    reaped = container.store.fail_stale_job_runs(
        job_type=TRIGGER_JOB_TYPE,
        now=utc_timestamp(container.clock.now()),
    )
    if reaped:
        _LOGGER.warning("trigger_stale_runs_reaped", count=reaped)
    ledger = JobLedger(store=container.store, clock=container.clock)
    service = TriggerService(
        store=container.store,
        clock=container.clock,
        stale_after_seconds=settings.trigger_stale_after_seconds,
    )
    runner = TriggerJobRunner(
        service=service,
        ledger=ledger,
        clock=container.clock,
        interval_seconds=settings.trigger_interval_seconds,
    )
    scheduler = TriggerDaemonScheduler(
        interval_seconds=settings.trigger_interval_seconds,
        job=runner.run_tick,
    )
    scheduler.start()
    return scheduler


def _start_position_monitor_scheduler(
    settings: Settings,
    *,
    container: Any,
) -> PositionMonitorDaemonScheduler:
    """Start the code-only 15 s protective/mark tick.

    A killed process can leave a ``position_monitor`` row RUNNING forever; the
    runner only ever claims the current interval window, so expired leases are
    repaired at start.
    """
    from oft.application.position_service import PositionService

    reaped = container.store.fail_stale_job_runs(
        job_type=POSITION_MONITOR_JOB_TYPE,
        now=utc_timestamp(container.clock.now()),
    )
    if reaped:
        _LOGGER.warning("position_monitor_stale_runs_reaped", count=reaped)
    ledger = JobLedger(store=container.store, clock=container.clock)
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    runner = PositionMonitorRunner(
        service,
        ledger,
        container.clock,
        interval_seconds=settings.position_monitor_interval_seconds,
    )
    scheduler = PositionMonitorDaemonScheduler(
        interval_seconds=settings.position_monitor_interval_seconds,
        job=runner.run_tick,
    )
    scheduler.start()
    return scheduler


def _start_management_scheduler(
    settings: Settings,
    state: ScheduleState,
    *,
    container: Any,
    scan_scheduler: ScanDaemonScheduler | None = None,
) -> ManagementDaemonScheduler:
    """Start the scan-aligned management cadence.

    The runner is rebuilt per fire from the active scan period, so a pending
    scan-period change is picked up at the next boundary. When no explicit
    ``OFT_MANAGEMENT_CADENCE`` overrides the scan period, the live scan
    schedule state is wired in so a pending change re-arms at its transition
    boundary. The durable ledger claims each ``(position, window)`` once, so
    overlap, restart, and a late fire can never duplicate a decision or order.

    ADR-0007 also requires one management run for the open positions after a
    restart; outside the test environment that catch-up runs once for the
    current window after the scan catch-up (ledger-deduped). Missed periodic
    windows are not replayed.
    """
    from oft.application.kill_switch import KillSwitch
    from oft.application.management_service import ManagementService

    # A killed process can leave per-position management rows RUNNING. Expire
    # (do not finish) them so the restart catch-up below reclaims the same rows
    # and replays their decisions idempotently; the periodic cadence can also
    # reclaim them later if the catch-up itself crashes.
    expired = container.store.expire_job_leases(
        job_type=POSITION_MANAGE_JOB_TYPE,
        now=utc_timestamp(container.clock.now()),
    )
    if expired:
        _LOGGER.warning("management_leases_expired", count=expired)
    ledger = JobLedger(store=container.store, clock=container.clock)
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings,
            store=container.store,
            clock=container.clock,
        ),
        runtime=container.llm_runtime,
        funding_settlement_enabled=settings.management_funding_settlement_enabled,
        risk_breach_enabled=settings.management_risk_breach_enabled,
    )

    def _current_state() -> ScheduleState:
        return scan_scheduler.state if scan_scheduler is not None else state

    def _period() -> str:
        if settings.management_cadence:
            return settings.management_cadence
        return _current_state().period_at(container.clock.now())

    def _run_current(window: datetime) -> Any:
        runner = _management_runner(service, ledger, container, settings, _period())
        return runner.run_window(window_start=window)

    scheduler = ManagementDaemonScheduler(
        clock=container.clock,
        job=_run_current,
        period_provider=_period,
        # An explicit management cadence is independent of the scan schedule;
        # only the scan-aligned default can use the live schedule state.
        state_provider=None if settings.management_cadence else _current_state,
    )
    # Mirror the scan catch-up: run the current window once for the open
    # positions after a restart. The ledger dedupes it, so a restart inside the
    # same window writes nothing new. Tests keep the startup path side-effect
    # free, matching the scan catch-up's env gate.
    if settings.env != "test":
        _catch_up_management_on_start(container, service, ledger, settings, state)
    scheduler.start()
    return scheduler


def _management_runner(
    service: Any,
    ledger: JobLedger,
    container: Any,
    settings: Settings,
    period: str,
) -> ManagementJobRunner:
    """One management runner for both the periodic fire and the restart catch-up."""
    return ManagementJobRunner(
        service,
        ledger,
        container.clock,
        period=period,
        funding_settlement_enabled=settings.management_funding_settlement_enabled,
        risk_breach_enabled=settings.management_risk_breach_enabled,
    )


def _catch_up_management_on_start(
    container: Any,
    service: Any,
    ledger: JobLedger,
    settings: Settings,
    state: ScheduleState,
) -> ManagementJobOutcome | None:
    """Run the current management window once after a restart (ADR-0007).

    Only the window containing the current clock instant is claimed, and only
    once: a second startup in the same window loses the ledger claim and writes
    nothing. Startup has already expired any RUNNING lease, so a crashed
    current-window row is reclaimed and its decision replayed. A failure is
    logged and never aborts daemon startup.
    """
    try:
        now = container.clock.now()
        period = settings.management_cadence or state.period_at(now)
        window = align_to_boundary(now, period)
        runner = _management_runner(service, ledger, container, settings, period)
        outcome = runner.run_window(window_start=window)
        _LOGGER.info(
            "management_restart_catch_up",
            window_start=utc_timestamp(window),
            status=outcome.status.value,
            reason=outcome.reason,
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 - startup must survive a catch-up failure
        _LOGGER.warning("management_restart_catch_up_failed", error=str(exc))
        return None


def _catch_up_on_start(container: Any, state: ScheduleState) -> None:
    ledger = JobLedger(store=container.store, clock=container.clock)
    runner = ScheduledScanRunner(container=container)
    # A crash can leave RUNNING rows behind. Resume an unexpired lease in
    # place (renewing it) and reclaim only expired leases.
    now = utc_timestamp(container.clock.now())
    for run in ledger.reconstruct("scan").running:
        if run.lease_expires_at > now:
            ledger.heartbeat(run.run_id, owner=run.lease_owner)
            runner.resume(run.run_id, period=state.period_at(container.clock.now()))
            continue
        reclaimed = ledger.claim(
            "scan",
            run.window_start,
            owner=run.lease_owner,
            run_id=run.run_id,
        )
        if reclaimed.claimed and reclaimed.reclaimed:
            runner.resume(run.run_id, period=state.period_at(container.clock.now()))

    scheduled = [
        run
        for run in container.store.list_job_runs("scan")
        if run.trigger_source == "SCHEDULED" and "#manual-" not in run.window_start
    ]
    last = (
        datetime.fromisoformat(scheduled[0].window_start.replace("Z", "+00:00"))
        if scheduled
        else None
    )
    period = state.period_at(container.clock.now())
    plan = ledger.catch_up(
        period=period, now=container.clock.now(), last_attempted_window_start=last
    )
    if plan.run_window is not None:
        runner.run_window(plan.run_window, period=period)
        last = plan.run_window
    # A slow catch-up can cross a boundary. Re-plan at the finishing clock so
    # the newly due window is run now instead of being skipped until the next
    # future boundary.
    period = state.period_at(container.clock.now())
    final_plan = ledger.catch_up(
        period=period, now=container.clock.now(), last_attempted_window_start=last
    )
    if final_plan.run_window is not None:
        runner.run_window(final_plan.run_window, period=period)


@daemon_app.command("stop")
def daemon_stop(
    json_output: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Signal the daemon to stop; safe when it is already offline."""
    settings = _settings_or_exit(json_output)
    pid = read_pid(settings)
    if pid is None or not pid_matches(settings, pid):
        if json_output:
            typer.echo(json.dumps({"ok": True, "data": {"state": "NOT_RUNNING"}}, indent=2))
        else:
            typer.echo("daemon is not running")
        return
    os.kill(pid, signal.SIGTERM)
    pid_path(settings).unlink(missing_ok=True)
    if json_output:
        typer.echo(json.dumps({"ok": True, "data": {"state": "STOPPED", "pid": pid}}, indent=2))
    else:
        typer.echo(f"stopped daemon (pid {pid})")


@daemon_app.command("status")
def daemon_status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
) -> None:
    """Show PID state plus the next and last UTC scan windows."""
    settings = _settings_or_exit(json_output)
    from oft.bootstrap import build_container

    container = build_container(settings)
    state = _schedule_state(settings, container=container, json_output=json_output)
    payload = _status_payload(settings, state, container=container)
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    payload,
                    request_id=new_request_id(),
                    generated_at=payload["generated_at"],
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    lines = [
        f"Daemon            {payload['state']}"
        + (f" (pid {payload['pid']})" if payload["pid"] is not None else ""),
        f"Schedule period   {payload['period']}",
        f"Next run (UTC)    {payload['next_run_at']}",
    ]
    last = payload["last_run"]
    if last is None:
        lines.append("Last run (UTC)    never")
    else:
        lines.append(
            f"Last run (UTC)    {last['run_id']} · {last['status']} · {last['finished_at']}"
        )
    typer.echo("\n".join(lines))


@daemon_app.command("logs")
def daemon_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, help="Tail line count.")] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the tail as JSON.")] = False,
) -> None:
    """Print the daemon's structured log tail."""
    settings = _settings_or_exit(json_output)
    path = log_path(settings)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        if json_output:
            typer.echo(json.dumps({"ok": True, "data": {"path": str(path), "lines": []}}, indent=2))
        else:
            typer.echo(f"no daemon log at {path}")
        return
    tail = text.splitlines()[-lines:]
    if json_output:
        typer.echo(json.dumps({"ok": True, "data": {"path": str(path), "lines": tail}}, indent=2))
    else:
        typer.echo("\n".join(tail))


__all__ = [
    "daemon_app",
    "daemon_logs",
    "daemon_start",
    "daemon_status",
    "daemon_stop",
    "log_path",
    "pid_path",
    "process_alive",
    "read_pid",
    "_run_settings_sync",
]
