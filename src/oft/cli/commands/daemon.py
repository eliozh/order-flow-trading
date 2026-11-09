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
    TRIGGER_JOB_TYPE,
    JobLedger,
    ScheduledScanRunner,
    TriggerJobRunner,
)
from oft.application.scheduler import (
    ScanDaemonScheduler,
    ScheduleState,
    TriggerDaemonScheduler,
    resolve_schedule_settings,
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


def _schedule_state(settings: Settings | None = None) -> ScheduleState:
    try:
        config = resolve_schedule_settings()
    except ValueError as exc:
        emit_error("CONFIG_INVALID", str(exc), json_output=False)
        raise typer.Exit(code=5) from exc
    default = ScheduleState(period=config.period, jitter_seconds_max=config.jitter_seconds)
    if settings is None:
        return default
    from oft.application.scheduler import load_schedule_state

    return load_schedule_state(schedule_state_path(settings), default=default)


def _last_scheduled_run(settings: Settings) -> Any:
    from oft.bootstrap import build_container

    container = build_container(settings)
    for run in container.store.list_job_runs("scan"):
        if run.trigger_source == "SCHEDULED":
            return run
    return None


def _status_payload(settings: Settings, state: ScheduleState) -> dict[str, Any]:
    from oft.clock import SystemClock

    pid = read_pid(settings)
    running = pid is not None and process_alive(pid)
    now = SystemClock().now()
    last = _last_scheduled_run(settings)
    schedule = state.next_run_payload(now)
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
    state = _schedule_state(settings)
    from oft.bootstrap import build_container

    container = build_container(settings, initialize=True)
    _run_live_ingestion(settings, replay_file, symbols, container=container)
    scheduler = _start_scheduler(settings, state, container=container)
    trigger_scheduler = _start_trigger_scheduler(settings, container=container)
    try:
        _run_uvicorn(settings)
    finally:
        trigger_scheduler.shutdown()
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
    state = _schedule_state(settings)
    payload = _status_payload(settings, state)
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
]
