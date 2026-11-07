"""``oft report list|show``: CALC metrics first, then the explicit LLM state."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

report_app = typer.Typer(
    help="CALC report commands (metrics-only; no LLM narrative).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _signed(value: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 0:
        return f"▲ +{value}"
    if number < 0:
        return f"▼ {value}"
    return str(value)


def _section_lines(metrics: dict[str, Any]) -> list[str]:
    cvd = metrics.get("cvd") or {}
    points = cvd.get("points") or []
    summary = cvd.get("summary") or {}
    profile = metrics.get("volume_profile") or {}
    oi = metrics.get("open_interest") or {}
    funding = metrics.get("funding") or {}
    liquidations = metrics.get("liquidations") or {}
    footprint = metrics.get("footprint") or {}
    gaps = metrics.get("gaps") or []

    latest_delta = points[-1]["delta"] if points else "0"
    fp_bars = footprint.get("bars") or []
    fp_cells = sum(len(bar.get("cells") or []) for bar in fp_bars)
    liq_count = liquidations.get("count", 0)
    coverage = liquidations.get("coverage", "NONE")
    oi_measured = bool(oi.get("points"))
    funding_measured = bool(funding.get("history"))
    oi_value = str(oi.get("delta_over_window", "0")) if oi_measured else "not measured"
    funding_value = str(funding.get("current_rate", "0")) if funding_measured else "not measured"
    oi_note = f" · {oi.get('sampling')}" if oi_measured else ""
    funding_note = f" · next {funding.get('next_funding_time')}" if funding_measured else ""

    return [
        f"  CVD (window)      {_signed(str(summary.get('cvd_change', '0')))}"
        f"    delta {_signed(str(summary.get('delta', '0')))}"
        f" · buy {summary.get('buy_volume', '-')} / sell {summary.get('sell_volume', '-')}",
        f"  Delta (latest)    {_signed(str(latest_delta))}",
        f"  POC               {profile.get('poc', '-')}",
        f"  VAH / VAL         {profile.get('vah', '-')} / {profile.get('val', '-')}",
        f"  Volume Profile    {len(profile.get('bins') or [])} bins"
        f" · {profile.get('bin_size', '-')} per bin",
        f"  Footprint         {len(fp_bars)} bars · {fp_cells} cells",
        f"  OI Δ              {oi_value}{oi_note}",
        f"  Funding           {funding_value}{funding_note}",
        f"  Liquidations      {liq_count} events · {coverage}",
        f"  Gaps              {len(gaps)}",
    ]


def render_report_table(data: dict[str, Any]) -> str:
    """Render CALC metrics before the explicit LLM-not-configured notice."""
    snapshot = data.get("snapshot") or {}
    metrics = data.get("metrics") or {}
    window = snapshot.get("window") or metrics.get("window") or {}
    lines = [
        f"REPORT {data.get('report_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('timeframe', '-')}",
        f"window {window.get('start', '-')} → {window.get('end', '-')}",
        f"generated {data.get('generated_at', '-')} · scan {data.get('scan_id', '-')}"
        f" · quality {data.get('data_quality', '-')}",
        "",
        "CALC",
        *_section_lines(metrics),
        "",
    ]
    llm = data.get("llm") or {}
    reason = llm.get("reason") or ("LLM not configured; no narrative or decision was generated.")
    notice = (
        reason
        if reason.lower().startswith("llm not configured")
        else f"LLM not configured — {reason}"
    )
    lines.append(notice)
    lines.append("No LLM call, order, or credential was required or recorded.")
    return "\n".join(lines)


@report_app.command("list")
def report_list(
    scan: Annotated[str | None, typer.Option("--scan", help="Filter to one scan ID.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """List CALC-only reports."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_reports(limit=limit, scan_id=scan)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no reports.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = data.get("items") or []
    if not items:
        typer.echo("No reports yet. Run: oft scan run --metrics-only --symbol BTCUSDT")
        return
    typer.echo(f"{'REPORT':<36}{'SYMBOL':<12}{'PERIOD':<8}QUALITY")
    for item in items:
        typer.echo(
            f"{str(item.get('report_id', '-')):<36}"
            f"{str(item.get('symbol', '-')):<12}"
            f"{str(item.get('timeframe', '-')):<8}"
            f"{item.get('data_quality', '-')}"
        )


@report_app.command("show")
def report_show(
    report_id: str,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Show one CALC report; CALC metrics always precede the LLM notice."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_report(report_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Report {report_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(render_report_table(data))
