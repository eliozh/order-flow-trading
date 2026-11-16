"""``oft daemon start|stop|status|logs``: supervisor-facing daemon control."""

from __future__ import annotations

import contextlib
import functools
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
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

# Module-size note: this file is deliberately above the 800-line soft ceiling.
# The daemon lifecycle commands share one security-critical contract -- the
# PID-file format, the start-time fence, the scheduler state handoff, and the
# detached restart capture -- so splitting them would duplicate that contract
# (and invite the two halves to drift apart).

daemon_app = typer.Typer(
    help="Daemon lifecycle (local API/UI, live ingestion, and the scan scheduler).",
    no_args_is_help=True,
    add_completion=False,
)


def pid_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "daemon.pid"


def log_path(settings: Settings) -> Path:
    return settings.data_dir / "logs" / "daemon.log"


# ``os.kill(0, ...)`` signals the caller's process group and ``os.kill(-1, ...)``
# signals every process the caller may signal. A PID file is untrusted input, so
# only an ordinary positive PID that cannot address a group is ever accepted.
_PID_MAX_FALLBACK = 4194304


@functools.cache
def _pid_max() -> int:
    try:
        raw = Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip()
        value = int(raw)
    except (OSError, ValueError):
        return _PID_MAX_FALLBACK
    return value if value > 1 else _PID_MAX_FALLBACK


def _valid_pid(pid: int) -> bool:
    """Whether ``pid`` names one ordinary process we may safely signal."""
    return 2 <= pid <= _pid_max()


def read_pid(settings: Settings) -> int | None:
    path = pid_path(settings)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if _valid_pid(pid) else None


def process_alive(pid: int) -> bool:
    if not _valid_pid(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_PS_TIMEOUT_SECONDS = 2.0


def _proc_start_time(pid: int) -> str | None:
    """The Linux ``/proc/<pid>/stat`` start time, or ``None`` if unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def _ps_executable() -> str | None:
    """Resolve ``ps`` directly so a PATH-shadowed binary cannot be executed."""
    for candidate in ("/bin/ps", "/usr/bin/ps"):
        if Path(candidate).is_file():
            return candidate
    return None


def _ps_start_time(pid: int) -> str | None:
    """``ps -o lstart=`` in the C locale, the portable (macOS) fence source."""
    executable = _ps_executable()
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _process_start_time(pid: int) -> str | None:
    """The process start time used by the PID fence.

    ``/proc`` is Linux-only and ADR-0001/0012 support launchd on macOS, so a
    missing or malformed ``/proc`` entry falls back to ``ps -o lstart=``
    (``LC_ALL=C``, short timeout, no shell) instead of failing the fence closed
    for every process on that platform. Both the recorded and re-read value use
    this same function, so the fence stays stable.
    """
    start = _proc_start_time(pid)
    if start is not None:
        return start
    return _ps_start_time(pid)


def pid_meta_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "daemon.pid.json"


def pid_matches(settings: Settings, pid: int) -> bool:
    """Verify the PID still names the daemon we started (start-time fence).

    Fail closed: the fence needs the recorded start time and a re-read of the
    current one (``/proc`` or the portable ``ps`` fallback). A
    missing/unreadable/corrupt meta file, an unrecorded start time, or an
    unavailable current start time all return ``False`` so control is refused
    rather than sent to a recycled PID.
    """
    if not process_alive(pid):
        return False
    try:
        meta = json.loads(pid_meta_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    expected = meta.get("start_time")
    if not isinstance(expected, str) or not expected:
        return False
    current = _process_start_time(pid)
    return current is not None and expected == current


def write_pid(settings: Settings) -> None:
    path = pid_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid = os.getpid()
    path.write_text(str(pid), encoding="utf-8")
    path.chmod(0o600)

    meta = pid_meta_path(settings)
    meta.write_text(
        json.dumps(
            {
                "pid": pid,
                "start_time": _process_start_time(pid),
                # Additive: ``system info`` derives uptime from this stamp.
                "started_at": utc_timestamp(datetime.now(UTC)),
            }
        ),
        encoding="utf-8",
    )
    meta.chmod(0o600)


def settings_or_exit(json_output: bool) -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def schedule_state_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "schedule.json"


def schedule_state(
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


# Compatibility aliases for callers that predate the public names. New code
# imports ``settings_or_exit`` / ``schedule_state`` directly.
_settings_or_exit = settings_or_exit
_schedule_state = schedule_state


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
    # ``start`` never signals the PID, so its availability guard is deliberately
    # conservative: refuse to start while any recorded PID is alive, even when
    # the start-time fence cannot verify it. Stop/restart keep the strict
    # fail-closed ``pid_matches`` fence because they send signals.
    if pid is not None and (pid_matches(settings, pid) or process_alive(pid)):
        emit_error(
            "DAEMON_ALREADY_RUNNING",
            f"daemon already running with pid {pid}",
            json_output=False,
        )
        raise typer.Exit(code=1)
    from oft.bootstrap import build_container

    container = build_container(settings, initialize=True)
    # Call the private alias so the existing test seam (which patches
    # ``_schedule_state``) stays effective; the public name is for new callers.
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

    def _build_service() -> Any:
        """Rebuild the management service from the effective settings.

        The adapters capture the key/base URL at construction, so a service
        built once would keep the startup provider forever. Building per run
        makes a rotated key or a changed provider/base URL apply without a
        restart.
        """
        container.refresh_llm_settings()
        return ManagementService(
            store=container.store,
            clock=container.clock,
            provider=container.provider_for(),
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

    service = _build_service()

    def _current_state() -> ScheduleState:
        return scan_scheduler.state if scan_scheduler is not None else state

    def _period() -> str:
        if settings.management_cadence:
            return settings.management_cadence
        return _current_state().period_at(container.clock.now())

    def _run_current(window: datetime) -> Any:
        runner = _management_runner(_build_service(), ledger, container, settings, _period())
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


def _emit_not_running(json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"ok": True, "data": {"state": "NOT_RUNNING"}}, indent=2))
    else:
        typer.echo("daemon is not running")


@daemon_app.command("stop")
def daemon_stop(
    json_output: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Signal the daemon to stop; safe when it is already offline."""
    settings = settings_or_exit(json_output)
    pid = read_pid(settings)
    if pid is None or not pid_matches(settings, pid):
        _emit_not_running(json_output)
        return
    # Re-check the start-time fence immediately before signalling: the PID may
    # have been recycled between the check above and ``os.kill`` (TOCTOU), and a
    # stale PID must never receive SIGTERM.
    if not pid_matches(settings, pid):
        _emit_not_running(json_output)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # The daemon exited between the fence check and the signal: mirror
        # ``_stop_daemon`` -- clear the stale artifacts and report NOT_RUNNING.
        _remove_pid_files(settings)
        _emit_not_running(json_output)
        return
    _remove_pid_files(settings)
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
    settings = settings_or_exit(json_output)
    from oft.bootstrap import build_container

    container = build_container(settings)
    state = schedule_state(settings, container=container, json_output=json_output)
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
    settings = settings_or_exit(json_output)
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


_RESTART_TIMEOUT_SECONDS = 10.0
_RESTART_POLL_SECONDS = 0.05
_CHILD_TERMINATE_TIMEOUT_SECONDS = 5.0
_STARTUP_OUTPUT_LIMIT = 500
# Read a larger bounded window than the reported tail so a redaction match (for
# example a long configured secret) is seen whole before the tail is truncated.
_STARTUP_OUTPUT_READ_BYTES = 8192
# C0/C1 controls (except tab) must never reach a terminal from child output.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _remove_pid_files(settings: Settings) -> None:
    """Remove both PID artifacts on a successful/already-gone stop path."""
    pid_path(settings).unlink(missing_ok=True)
    pid_meta_path(settings).unlink(missing_ok=True)


def _reap_child(pid: int) -> bool:
    """Reap ``pid`` if it is our child and has exited (never blocks)."""
    if not _valid_pid(pid):
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return False
    return reaped == pid


def _stop_daemon(
    settings: Settings,
    pid: int,
    *,
    timeout_seconds: float = _RESTART_TIMEOUT_SECONDS,
) -> bool:
    """SIGTERM the daemon and wait for it to exit; never SIGKILL.

    The start-time fence is re-checked immediately before ``os.kill`` so a PID
    recycled while the caller prompted is never signalled. A child of this
    process is reaped while polling, so a zombie cannot make ``process_alive``
    report it as running until the timeout. The stale PID artifacts are removed
    on success or when the process is already gone; a timeout keeps them so
    ``daemon stop`` / ``daemon status`` can retry against the still-live daemon.
    A non-positive PID is never signalled: ``os.kill(0, ...)`` and
    ``os.kill(-1, ...)`` address a process group / every process.
    """
    if not _valid_pid(pid):
        return False
    if not pid_matches(settings, pid):
        # The caller verified the fence earlier; refuse without signalling if
        # it no longer matches (recycled PID or a vanished meta file).
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid_files(settings)
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _reap_child(pid) or not process_alive(pid):
            _remove_pid_files(settings)
            return True
        time.sleep(_RESTART_POLL_SECONDS)
    # The daemon may still be alive: keep the PID artifacts so a later
    # ``daemon stop``/``status`` can observe and retry it.
    return False


def _restart_stderr_path(settings: Settings) -> Path:
    """Where a detached restart child's stderr (only) is captured."""
    return settings.data_dir / "run" / "restart-stderr.log"


def _spawn_detached_daemon(settings: Settings) -> subprocess.Popen[bytes]:
    """Start a detached daemon process that outlives the restarting CLI.

    The module entrypoint is preferred over a PATH-discovered ``oft`` so a
    shadowed executable cannot hijack the restart. structlog already writes the
    daemon log, so the child's stdout is discarded; only stderr (low-volume
    uvicorn startup/error output) goes to a private (0600) file, truncated per
    restart, so a startup failure can be reported instead of vanishing into
    ``DEVNULL``.
    """
    log_file = _restart_stderr_path(settings)
    log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _private_opener(path: str | os.PathLike[str], flags: int) -> int:
        # ``O_CREAT`` applies ``0o600`` at creation (and ``fchmod`` fixes a file
        # left by an older version), closing the brief world-readable window
        # that ``open("wb")`` followed by ``chmod`` leaves.
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    # The child holds its own dup of the descriptor after fork/exec, so the
    # parent handle is closed as soon as the spawn returns.
    with open(log_file, "wb", opener=_private_opener) as handle:
        child = subprocess.Popen(
            [sys.executable, "-m", "oft.cli.main", "daemon", "start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=handle,
            start_new_session=True,
            close_fds=True,
        )
    return child


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    """Terminate a spawned daemon whose startup did not report a new PID."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=_CHILD_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=_CHILD_TERMINATE_TIMEOUT_SECONDS)


def _startup_output_tail(settings: Settings, *, limit: int = _STARTUP_OUTPUT_LIMIT) -> str:
    """A bounded, fully redacted, control-stripped tail of the child's output.

    Up to 8 KiB is read and redacted *before* truncation, so a redaction match
    (notably a long configured secret) is seen whole and never emitted as a
    partial value. The full ``system.py`` redactor is reused (imported lazily to
    avoid the module import cycle), so headers, cookies, token shapes, and
    configured secret values are all removed -- not just the subset
    ``redact_secrets`` covers. Only the last ``limit`` characters are kept.
    """
    from oft.cli.commands.system import _configured_secret_values, redact_record

    path = _restart_stderr_path(settings)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _STARTUP_OUTPUT_READ_BYTES))
            raw = handle.read(_STARTUP_OUTPUT_READ_BYTES)
    except OSError:
        return f"child output unavailable; see {path}"
    decoded = _CONTROL_CHARS.sub("", raw.decode("utf-8", errors="replace"))
    text = str(redact_record(decoded, secret_values=_configured_secret_values(settings))).strip()
    if not text:
        return f"no child output; see {path}"
    return f"{text[-limit:]} (see {path})"


def _child_exit_code(child: Any) -> int | None:
    """The child's exit code when it has exited, else ``None``.

    Tests replace the spawned child with an opaque sentinel, so a missing
    ``poll`` is treated as "still running" rather than an error.
    """
    poll = getattr(child, "poll", None)
    if poll is None:
        return None
    code = poll()
    return code if isinstance(code, int) else None


def _wait_for_new_pid(
    settings: Settings,
    previous_pid: int,
    *,
    timeout_seconds: float = _RESTART_TIMEOUT_SECONDS,
    child: Any | None = None,
) -> int | None:
    """Wait for the restarted daemon to publish a live PID different from the old.

    When ``child`` is supplied, an already exited child fails the wait
    immediately so the caller reports its exit code instead of waiting the full
    timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if child is not None and _child_exit_code(child) is not None:
            return None
        pid = read_pid(settings)
        if pid is not None and pid != previous_pid and pid_matches(settings, pid):
            return pid
        time.sleep(_RESTART_POLL_SECONDS)
    return None


def _wait_for_ready(
    settings: Settings,
    *,
    timeout_seconds: float = _RESTART_TIMEOUT_SECONDS,
    child: Any | None = None,
) -> bool:
    """Poll ``GET /ready`` directly over loopback until HTTP 200 or the deadline.

    ``http.client`` is used instead of ``urllib`` so ambient ``http_proxy`` /
    ``HTTP_PROXY`` settings (with loopback outside ``no_proxy``) cannot route
    the readiness probe through a proxy -- which would time out and make a
    healthy restart look failed. Connection errors, malformed responses, and
    any non-200 status are retried. When ``child`` is supplied, an already
    exited child fails the wait immediately so the caller reports its exit code
    instead of waiting the full timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        if child is not None and child.poll() is not None:
            return False
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(settings.host, settings.port, timeout=1.0)
            connection.request("GET", "/ready")
            response = connection.getresponse()
            if response.status == 200:
                return True
        except (OSError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        if time.monotonic() >= deadline:
            return False
        time.sleep(_RESTART_POLL_SECONDS)


def _restart_warning(open_positions: int, position_ids: list[str]) -> str:
    if open_positions == 0:
        return "No open positions are affected by this restart."
    if open_positions == 1:
        summary = "1 open position remains open; restarting the daemon never closes it."
    else:
        summary = (
            f"{open_positions} open positions remain open; restarting the daemon never closes them."
        )
    lines = [f"warning: {summary}"]
    if position_ids:
        shown = ", ".join(position_ids)
        suffix = " ..." if open_positions > len(position_ids) else ""
        lines.append(f"Open positions: {shown}{suffix}")
    return "\n".join(lines)


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


# Restart is deliberately colocated with start/stop/status rather than split
# into its own module: it reuses the same PID-file, ``pid_path``, and
# ``pid_matches`` start-time fence contract, and splitting would duplicate that
# security-critical lifecycle logic across modules.
@daemon_app.command("restart")
def daemon_restart(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the restart result as JSON.")
    ] = False,
) -> None:
    """Stop and start the daemon, warning about open positions first.

    The restart spawns ``daemon start`` with the current environment and
    defaults; flags passed to an earlier ``daemon start`` (``--host``,
    ``--port``, ``--allow-non-loopback``, ``--replay-file``, ``--symbols``)
    are not persisted and are therefore not reused.
    """
    settings = settings_or_exit(json_output)
    pid = read_pid(settings)
    if pid is None or not pid_matches(settings, pid):
        emit_error(
            "DAEMON_NOT_RUNNING",
            "daemon is not running; start it with `oft daemon start`",
            json_output=json_output,
        )
        raise typer.Exit(code=1)

    from oft.bootstrap import build_container

    try:
        container = build_container(settings)
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    # Read-only: a restart must never close or mutate an open position. The
    # count is exact; only the first 10 ids are read for the warning.
    open_position_count = container.store.count_positions(state="OPEN")
    open_position_ids = [
        position.position_id for position in container.store.list_positions(state="OPEN", limit=10)
    ]
    warning = _restart_warning(open_position_count, open_position_ids)
    # Keep stdout a clean envelope in JSON mode.
    typer.echo(warning, err=json_output)

    if not yes:
        if not _stdin_is_tty():
            emit_error(
                "VALIDATION_FAILED",
                "non-interactive: pass --yes to confirm daemon restart",
                json_output=json_output,
            )
            raise typer.Exit(code=2)
        if not typer.confirm(
            "restart the daemon? it stops local management until it starts",
            default=False,
            err=json_output,
        ):
            if json_output:
                typer.echo(
                    json.dumps(
                        success_envelope(
                            {"state": "CANCELLED", "pid": pid},
                            request_id=new_request_id(),
                            generated_at=utc_timestamp(container.clock.now()),
                            provenance="CALC",
                            data_quality="COMPLETE",
                        ),
                        indent=2,
                    )
                )
            else:
                typer.echo("restart cancelled")
            raise typer.Exit(code=0)

    if not _stop_daemon(settings, pid, timeout_seconds=_RESTART_TIMEOUT_SECONDS):
        emit_error(
            "INTERNAL_ERROR",
            f"daemon pid {pid} did not stop within "
            f"{_RESTART_TIMEOUT_SECONDS:g}s; it was not force-killed",
            json_output=json_output,
        )
        raise typer.Exit(code=1)

    try:
        child = _spawn_detached_daemon(settings)
    except OSError as exc:
        emit_error(
            "INTERNAL_ERROR",
            f"daemon stopped but the restart process could not start: {type(exc).__name__}",
            json_output=json_output,
        )
        raise typer.Exit(code=1) from exc

    new_pid = _wait_for_new_pid(
        settings, pid, timeout_seconds=_RESTART_TIMEOUT_SECONDS, child=child
    )
    if new_pid is None:
        exited = _child_exit_code(child)
        _terminate_child(child)
        if exited is not None:
            cause = f"restarted daemon exited during startup (exit {exited}); "
        else:
            cause = f"daemon did not come up within {_RESTART_TIMEOUT_SECONDS:g}s after a restart; "
        emit_error(
            "INTERNAL_ERROR",
            cause + _startup_output_tail(settings),
            json_output=json_output,
        )
        raise typer.Exit(code=1)

    # A published PID only proves the process started; wait for the API to be
    # ready so a child that crashes or hangs during startup is not reported as
    # a successful restart. An early child exit fails the wait immediately.
    if not _wait_for_ready(settings, timeout_seconds=_RESTART_TIMEOUT_SECONDS, child=child):
        exited = child.poll()
        _terminate_child(child)
        if exited is not None:
            cause = f"restarted daemon exited during startup (exit {exited}); "
        else:
            cause = (
                f"daemon published pid {new_pid} but was not ready within "
                f"{_RESTART_TIMEOUT_SECONDS:g}s; "
            )
        emit_error(
            "INTERNAL_ERROR",
            cause + _startup_output_tail(settings),
            json_output=json_output,
        )
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    {
                        "state": "RESTARTED",
                        "previous_pid": pid,
                        "pid": new_pid,
                        "open_positions": open_position_count,
                    },
                    request_id=new_request_id(),
                    generated_at=utc_timestamp(container.clock.now()),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    typer.echo(f"restarted daemon (pid {pid} → {new_pid})")


__all__ = [
    "daemon_app",
    "daemon_logs",
    "daemon_restart",
    "daemon_start",
    "daemon_status",
    "daemon_stop",
    "log_path",
    "pid_matches",
    "pid_meta_path",
    "pid_path",
    "process_alive",
    "read_pid",
    "schedule_state",
    "settings_or_exit",
    "write_pid",
    "_run_settings_sync",
]
