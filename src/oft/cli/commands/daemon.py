"""``oft daemon start|stop|status|logs``: supervisor-facing daemon control."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.jobs import JobLedger, ScheduledScanRunner
from oft.application.scheduler import (
    ScanDaemonScheduler,
    ScheduleState,
    resolve_schedule_settings,
)
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, Settings, load_settings
from oft.domain.time import utc_timestamp

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


def write_pid(settings: Settings) -> None:
    path = pid_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(str(os.getpid()), encoding="utf-8")
    path.chmod(0o600)


def _settings_or_exit(json_output: bool) -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _schedule_state() -> ScheduleState:
    try:
        config = resolve_schedule_settings()
    except ValueError as exc:
        emit_error("CONFIG_INVALID", str(exc), json_output=False)
        raise typer.Exit(code=5) from exc
    return ScheduleState(period=config.period, jitter_seconds_max=config.jitter_seconds)


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
    """Start the daemon, live ingestion, and the UTC scan scheduler."""
    # Imported lazily: ``main`` imports this module to register the command tree.
    from oft.cli.main import _resolve_serve_settings, _run_live_ingestion, _run_uvicorn

    settings = _resolve_serve_settings(host, port, allow_non_loopback)
    state = _schedule_state()
    _run_live_ingestion(settings, replay_file, symbols)
    scheduler = _start_scheduler(settings, state)
    try:
        _run_uvicorn(settings)
    finally:
        scheduler.shutdown()
        pid_path(settings).unlink(missing_ok=True)


def _start_scheduler(settings: Settings, state: ScheduleState) -> ScanDaemonScheduler:
    from oft.bootstrap import build_container

    container = build_container(settings, initialize=True)
    runner = ScheduledScanRunner(container=container)
    scheduler = ScanDaemonScheduler(
        clock=container.clock,
        state=state,
        job=lambda window: runner.run_window(window, period=state.period_at(container.clock.now())),
    )
    if settings.env != "test":
        _catch_up_on_start(container, state)
    scheduler.start()
    write_pid(settings)
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
            runner.resume(run.run_id, period=state.period)
            continue
        reclaimed = ledger.claim(
            "scan",
            run.window_start,
            owner=run.lease_owner,
            run_id=run.run_id,
        )
        if reclaimed.claimed and reclaimed.reclaimed:
            runner.resume(run.run_id, period=state.period)

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
    plan = ledger.catch_up(
        period=state.period, now=container.clock.now(), last_attempted_window_start=last
    )
    if plan.run_window is not None:
        runner.run_window(plan.run_window, period=state.period)


@daemon_app.command("stop")
def daemon_stop(
    json_output: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Signal the daemon to stop; safe when it is already offline."""
    settings = _settings_or_exit(json_output)
    pid = read_pid(settings)
    if pid is None or not process_alive(pid):
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
    state = _schedule_state()
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
