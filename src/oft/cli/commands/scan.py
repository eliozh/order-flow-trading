"""``oft scan run|status|history``."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, fields
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


def _is_batch_payload(data: dict[str, Any]) -> bool:
    """A batch payload has per-symbol rows but no single-scan identity."""
    return (
        isinstance(data.get("symbols"), list)
        and isinstance(data.get("scan"), dict) is False
        and "symbol" not in data
        and "status" in data
    )


def _scan_record(data: dict[str, Any]) -> ScanRecord:
    """Build a ScanRecord from a payload that may carry additive batch fields."""
    record_fields = {field.name for field in fields(ScanRecord)}
    return ScanRecord(**{key: value for key, value in data.items() if key in record_fields})


def _render_run(data: dict[str, Any]) -> str:
    if _is_batch_payload(data):
        return _render_batch(data)
    raw_scan = data.get("scan")
    scan_data = raw_scan if isinstance(raw_scan, dict) else data
    lines = [render_scan_table(_scan_record(scan_data))]
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


def _render_batch(data: dict[str, Any]) -> str:
    lines = [
        f"Scan              {data.get('scan_id', '-')}",
        f"Status            {data.get('status', '-')}",
        f"Trigger           {data.get('trigger_source', '-')} · period {data.get('period', '-')}",
        f"Symbols           {data.get('symbols_completed', 0)}/{data.get('symbols_total', 0)}"
        f" · {data.get('symbols_degraded', 0)} degraded"
        f" · {data.get('symbols_failed', 0)} failed",
        f"Cost              ${data.get('total_cost_usd', '0')}"
        f" · {data.get('total_tokens', 0)} tokens"
        + (" · unpriced" if data.get("cost_unpriced") else ""),
        "",
    ]
    for row in data.get("symbols") or []:
        if not isinstance(row, dict):
            continue
        result = row.get("stage", "-")
        llm = row.get("llm_status") or ("not configured" if row.get("stage") == "DONE" else "-")
        lines.append(
            f"{str(row.get('symbol', '-')):<12} LLM {str(llm):<10} {str(result):<9}"
            f" {row.get('report_id') or row.get('error_message') or '-'}"
        )
    delivery = data.get("delivery") or {}
    if isinstance(delivery, dict):
        lines.append("")
        lines.append(
            "Telegram          " + ("delivered" if delivery.get("configured") else "not configured")
        )
    return "\n".join(lines)


def _is_partial(data: dict[str, Any]) -> bool:
    status = str(data.get("status") or "")
    if status == "PARTIAL":
        return True
    return bool(data.get("symbols_degraded") or data.get("symbols_failed"))


def _render_detail(data: dict[str, Any]) -> str:
    if _is_batch_payload(data):
        return _render_batch(data)
    lines = [_render_run(data)]
    symbols = data.get("symbols")
    if isinstance(symbols, list) and symbols:
        lines.append("")
        lines.append("Stages")
        for row in symbols:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"  {str(row.get('symbol', '-')):<12} {str(row.get('stage', '-')):<9}"
                f" LLM {str(row.get('llm_status') or '-'):<10}"
                f" {row.get('report_id') or '-'}"
            )
    return "\n".join(lines)


@scan_app.command("run")
def run(
    fixture: Annotated[
        Path | None,
        typer.Option("--fixture", help="Legacy aggTrades CSV fixture debug path."),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option(
            "--symbol",
            help="Single contract symbol; omit to scan every starred symbol.",
        ),
    ] = None,
    period: Annotated[str, typer.Option("--period", help="Metric period, e.g. 4h.")] = "4h",
    source: Annotated[
        str,
        typer.Option("--source", help="Data source for a manual scan (backfill|live)."),
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
        if symbol is None:
            emit_error(
                "VALIDATION_FAILED",
                "--fixture scans require --symbol.",
                json_output=json_output,
            )
            raise typer.Exit(code=1)
        payload: dict[str, Any] = {
            "fixture_path": str(fixture),
            "symbol": symbol,
            "period": period,
            "metrics_only": metrics_only,
        }
        key = None
    else:
        payload = {
            "period": period,
            "source": source,
            "metrics_only": metrics_only,
        }
        if symbol is not None:
            payload["symbol"] = symbol
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
    if _is_partial(data):
        raise typer.Exit(code=3)


def _show_scan(settings: Any, scan_id: str, json_output: bool) -> None:
    """Fetch and render one scan; shared by ``status`` and ``show``."""
    try:
        envelope = ApiClient.from_settings(settings).get_scan(scan_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Scan {scan_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(_render_detail(data))


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
    _show_scan(settings, scan_id, json_output)


@scan_app.command("show")
def scan_show(
    scan_id: Annotated[str, typer.Argument(help="Scan or batch ID.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Show one scan (or batch) with its per-symbol stages."""
    settings = _settings_or_exit(json_output)
    _show_scan(settings, scan_id, json_output)


def _emit_schedule(settings: Any, json_output: bool) -> None:
    """Show the UTC next/last run state without requiring a daemon."""
    from oft.bootstrap import build_container
    from oft.domain.time import utc_timestamp

    # The persisted period is resolved before any environment validation is
    # attempted, so a stale ``OFT_SCAN_PERIOD`` cannot fail a valid saved value.
    container = build_container(settings)
    from oft.cli.commands.daemon import _schedule_state

    state = _schedule_state(settings, container=container, json_output=json_output)
    now = container.clock.now()
    state = state.promote(now)
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


def _format_duration(started_at: Any, finished_at: Any) -> str:
    if not started_at:
        return "-"
    if not finished_at:
        return "running"
    try:
        from datetime import datetime

        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    seconds = max(0, int((finished - started).total_seconds()))
    return f"{seconds // 60}:{seconds % 60:02d}"


@scan_app.command("history")
def scan_history(
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """List recent scan batches."""
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
    typer.echo(f"{'SCAN ID':<34}{'DURATION':<10}{'SYMBOLS':<9}{'STATUS':<10}{'COST':<9}TELEGRAM")
    for item in items:
        delivery = item.get("delivery") or {}
        telegram = "delivered" if delivery.get("configured") else "not configured"
        typer.echo(
            f"{str(item.get('scan_id', '-')):<34}"
            f"{_format_duration(item.get('started_at'), item.get('finished_at')):<10}"
            f"{item.get('symbols_completed', 0)}/{item.get('symbols_total', 0):<7}"
            f"{str(item.get('status', '-')):<10}"
            f"${item.get('total_cost_usd', item.get('cost_usd', '0')):<8}"
            f"{telegram}"
        )
