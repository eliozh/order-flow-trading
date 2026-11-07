"""``oft scan run|status|history``."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.cli.output import render_scan_table
from oft.config import ConfigError, load_settings
from oft.ports.store import ScanRecord

scan_app = typer.Typer(
    help="Scan commands (manual metrics-only scans; fixture scan for debugging).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit_scan(record: ScanRecord, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    asdict(record),
                    request_id=new_request_id(),
                    generated_at=record.finished_at,
                    provenance="CALC",
                    data_quality=record.data_quality,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(render_scan_table(record))


def _render_run(data: dict[str, Any]) -> str:
    raw_scan = data.get("scan")
    scan_data = raw_scan if isinstance(raw_scan, dict) else data
    lines = [render_scan_table(ScanRecord(**scan_data))]
    report = data.get("report")
    if isinstance(report, dict):
        llm = report.get("llm") or {}
        lines.append("")
        lines.append(f"Report            {report.get('report_id', '-')}")
        lines.append(f"Report quality    {report.get('data_quality', '-')}")
        if llm.get("configured"):
            lines.append(
                f"LLM               {llm.get('validation_status', '-')}"
                f" · opportunities {', '.join(llm.get('opportunity_ids') or []) or 'none'}"
            )
        else:
            lines.append("LLM               not configured (metrics-only CALC report)")
    return "\n".join(lines)


@scan_app.command("run")
def run(
    fixture: Annotated[
        Path | None,
        typer.Option("--fixture", help="Legacy aggTrades CSV fixture debug path."),
    ] = None,
    symbol: Annotated[str, typer.Option("--symbol", help="Contract symbol.")] = "BTCUSDT",
    period: Annotated[str, typer.Option("--period", help="Metric period, e.g. 4h.")] = "4h",
    source: Annotated[
        str, typer.Option("--source", help="Data source for a manual scan (backfill).")
    ] = "backfill",
    metrics_only: Annotated[
        bool,
        typer.Option(
            "--metrics-only/--no-metrics-only",
            help="Force a CALC-only scan; the default uses the configured provider.",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Provider override for this scan (stub)."),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Reuse a key for safe scripted retries."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Run one manual scan through the daemon (the only writer)."""
    settings = _settings_or_exit(json_output)
    if fixture is not None:
        payload: dict[str, Any] = {
            "fixture_path": str(fixture),
            "symbol": symbol,
            "period": period,
            "metrics_only": metrics_only,
        }
        key = None
    else:
        payload = {
            "symbol": symbol,
            "period": period,
            "source": source,
            "metrics_only": metrics_only,
        }
        if provider is not None:
            payload["provider"] = provider
        key = idempotency_key or f"cli-{uuid.uuid4().hex}"

    try:
        envelope = ApiClient.from_settings(settings).create_scan(payload, idempotency_key=key)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no scan data.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(_render_run(data))


@scan_app.command("status")
def scan_status(
    scan_id: Annotated[
        str | None,
        typer.Argument(help="Scan ID; omit to show the scheduled scan state."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Read one scan through the daemon API, or show the scan schedule."""
    settings = _settings_or_exit(json_output)
    if scan_id is None:
        _emit_schedule(settings, json_output)
        return

    try:
        envelope = ApiClient.from_settings(settings).get_scan(scan_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Scan {scan_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    _emit_scan(ScanRecord(**data), json_output)


def _emit_schedule(settings: Any, json_output: bool) -> None:
    """Show the UTC next/last run state without requiring a daemon."""
    from oft.application.scheduler import resolve_schedule_settings
    from oft.bootstrap import build_container
    from oft.domain.time import utc_timestamp

    try:
        resolve_schedule_settings()
    except ValueError as exc:
        emit_error("CONFIG_INVALID", str(exc), json_output=json_output)
        raise typer.Exit(code=5) from exc

    container = build_container(settings)
    from oft.cli.commands.daemon import _schedule_state

    state = _schedule_state(settings)
    now = container.clock.now()
    last = next(
        (run for run in container.store.list_job_runs("scan") if run.trigger_source == "SCHEDULED"),
        None,
    )
    data: dict[str, Any] = {
        "timezone": "UTC",
        "period": state.period_at(now),
        "pending_period": state.pending_period or "",
        "next_run_at": utc_timestamp(state.next_run(now)),
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
    }
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    data,
                    request_id=new_request_id(),
                    generated_at=utc_timestamp(now),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    lines = [
        f"Schedule period   {data['period']} (UTC boundaries)",
        f"Next run (UTC)    {data['next_run_at']}",
    ]
    if data["last_run"] is None:
        lines.append("Last run (UTC)    never")
    else:
        last_run = data["last_run"]
        lines.append(
            f"Last run (UTC)    {last_run['run_id']} · {last_run['status']} · "
            f"{last_run['finished_at']}"
        )
    typer.echo("\n".join(lines))


@scan_app.command("history")
def scan_history(
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """List recent scans."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_scans(limit=limit)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no scans.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = data.get("items") or []
    if not items:
        typer.echo("No scans yet. Run: oft scan run --metrics-only --symbol BTCUSDT")
        return
    typer.echo(f"{'SCAN ID':<28}{'SYMBOL':<12}{'PERIOD':<8}{'STATUS':<10}QUALITY")
    for item in items:
        typer.echo(
            f"{str(item.get('scan_id', '-')):<28}"
            f"{str(item.get('symbol', '-')):<12}"
            f"{str(item.get('period', '-')):<8}"
            f"{str(item.get('status', '-')):<10}"
            f"{item.get('data_quality', '-')}"
        )
