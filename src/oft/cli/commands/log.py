"""``oft log list|show|stats|compare|export|review``: the closed-trade journal."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

ADVISORY_LABEL = "ADVISORY ONLY — suggests, never changes rules/prompts/risk or orders"

log_app = typer.Typer(
    help="Closed-trade journal (read-only; PnL is fixed by code).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _render_trade(data: dict[str, Any]) -> str:
    signal = data.get("signal") or {}
    quality = data.get("decision_quality") or {}
    data_quality = data.get("data_quality") or {}
    provenance = data.get("provenance") or {}
    lines = [
        f"TRADE {data.get('trade_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · position {data.get('position_id', '-')}",
        f"opened {data.get('opened_at', '-')} · closed {data.get('closed_at', '-')}",
        f"entry {data.get('entry_avg_price', '-')} · exit {data.get('exit_avg_price', '-')}"
        f" · size {data.get('size', '-')}",
        f"gross {data.get('gross_pnl_usd', '-')} · fees {data.get('fees_usd', '-')}"
        f" · funding {data.get('funding_usd', '-')} · net {data.get('net_pnl_usd', '-')}"
        f" · return {data.get('return_pct', '-')}%",
        f"r_multiple {data.get('r_multiple')} · mae {data.get('mae_usd', '-')}"
        f" · mfe {data.get('mfe_usd', '-')}",
        "outcome " + ", ".join(str(tag) for tag in data.get("outcome_tags") or [])
        if data.get("outcome_tags")
        else "outcome -",
        f"signal scan={signal.get('scan_id')} report={signal.get('report_id')}"
        f" opportunity={signal.get('opportunity_id')} trigger={signal.get('trigger_id')}"
        f" decision={signal.get('decision_id')}",
        f"review matched_rules={quality.get('matched_rules')}"
        f" confidence={quality.get('confidence')} · {quality.get('notes', '')}",
        f"data_quality {data_quality.get('status', '-')}"
        + (
            " · reasons: " + "; ".join(str(item) for item in data_quality.get("reasons") or [])
            if data_quality.get("reasons")
            else ""
        ),
        f"provenance calc={provenance.get('calc_metric_version', '-')}"
        f" · {provenance.get('llm_provider', '-')}/{provenance.get('llm_model', '-')}"
        f" · prompt v{provenance.get('prompt_version', '-')}",
    ]
    return "\n".join(lines)


@log_app.command("list")
def log_list(
    symbol: Annotated[str | None, typer.Option("--symbol", help="Filter by symbol.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List closed trades."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_trade_logs(
            symbol=symbol.upper() if symbol else None, limit=limit
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = (envelope.get("data") or {}).get("items") or []
    if not items:
        typer.echo("No closed trades.")
        return
    typer.echo(f"{'TRADE':<24}{'SYMBOL':<10}{'SIDE':<7}{'CLOSED_AT':<28}{'NET':<16}FEES")
    for item in items:
        typer.echo(
            f"{str(item.get('trade_id', '-')):<24}"
            f"{str(item.get('symbol', '-')):<10}"
            f"{str(item.get('side', '-')):<7}"
            f"{str(item.get('closed_at', '-')):<28}"
            f"{str(item.get('net_pnl_usd', '-')):<16}"
            f"{item.get('fees_usd', '-')}"
        )


@log_app.command("show")
def log_show(
    trade_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one closed trade: PnL terms, outcome tags, and data-quality reasons."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_trade_log(trade_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Trade {trade_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_trade(data))


def _render_stats(data: dict[str, Any]) -> str:
    period = data.get("period") or {}
    if period:
        period_text = (
            f"period {period.get('label', '-')} ({period.get('start', '-')}"
            f" .. {period.get('end', '-')}, basis {period.get('basis', '-')})"
        )
    else:
        period_text = "period all trades"
    status = data.get("status", "-")
    lines = [f"JOURNAL STATS · {period_text}", f"status {status}"]
    if status == "UNDEFINED":
        lines.append(str(data.get("reason") or "metrics undefined"))
    else:
        lines.append(
            f"sample size {data.get('sample_size', 0)} · denominator {data.get('denominator', 0)}"
        )
    lines.append(
        f"wins {data.get('wins', 0)} · losses {data.get('losses', 0)} · ties {data.get('ties', 0)}"
    )
    win_rate = data.get("win_rate")
    expectancy = data.get("expectancy_usd")
    avg_r = data.get("avg_r") or {}
    avg_r_value = avg_r.get("value")
    lines.append(f"win rate {win_rate if win_rate is not None else 'N/A'}")
    lines.append(
        f"expectancy {expectancy if expectancy is not None else 'N/A'} USD"
        f" · avg R {avg_r_value if avg_r_value is not None else 'N/A'}"
        f" ({avg_r.get('samples', 0)} sample(s),"
        f" {avg_r.get('excluded_null_r', 0)} null-R excluded)"
    )
    totals = data.get("totals") or {}
    lines.append(
        f"totals gross {totals.get('gross_pnl_usd') or 'N/A'}"
        f" · fees {totals.get('fees_usd') or 'N/A'}"
        f" · funding {totals.get('funding_usd') or 'N/A'}"
        f" · net {totals.get('net_pnl_usd') or 'N/A'}"
    )
    exclusions = data.get("exclusions") or {}
    lines.append(
        f"excluded open positions {exclusions.get('open_positions', 0)}"
        f" · partial trades {exclusions.get('partial_trades', 0)}"
        f" · unreadable {exclusions.get('unreadable_trades', 0)}"
        f" · outside period {exclusions.get('outside_period', 0)}"
    )
    lines.append("definitions:")
    for name, text in (data.get("definitions") or {}).items():
        lines.append(f"  {name}: {text}")
    return "\n".join(lines)


@log_app.command("stats")
def log_stats(
    period: Annotated[
        str | None,
        typer.Option("--period", help="Window: Nd days, Nw weeks, Nm months (30 days)."),
    ] = None,
    symbol: Annotated[str | None, typer.Option("--symbol", help="Filter by symbol.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Deterministic PnL, win-rate, expectancy, and average-R statistics."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).journal_stats(
            period=period, symbol=symbol.upper() if symbol else None
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_stats(data))


def _render_compare(data: dict[str, Any]) -> str:
    lines = [
        f"COMPARE {data.get('trade_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · data_quality {data.get('data_quality', '-')}"
    ]
    zone = data.get("zone") or {}
    zone_line = (
        f"zone {zone.get('status', '-')} · entry {zone.get('entry') or 'N/A'}"
        f" · low {zone.get('zone_low') or 'N/A'} · high {zone.get('zone_high') or 'N/A'}"
    )
    if zone.get("distance_bps") is not None:
        zone_line += f" · distance {zone['distance_bps']:.4f} bps"
    lines.append(zone_line)
    slippage = data.get("slippage") or {}
    bps = slippage.get("bps")
    if bps is None:
        lines.append(f"slippage N/A · {slippage.get('formula', '')}".rstrip())
    else:
        lines.append(
            f"slippage {bps:.4f} bps · reference {slippage.get('reference_price')}"
            f" as_of {slippage.get('reference_as_of')}"
        )
    touch = data.get("first_touch") or {}
    kind = touch.get("kind")
    touch_line = (
        f"first_touch {kind if kind is not None else 'NONE'}"
        f" at {touch.get('at') or 'N/A'} · {touch.get('status', '-')}"
    )
    if touch.get("ambiguous"):
        touch_line += " · AMBIGUOUS_BAR"
    lines.append(touch_line)
    decision = data.get("decision") or {}
    decision_line = (
        f"decision {decision.get('decision') or 'N/A'}"
        f" · direction_agrees {decision.get('direction_agrees')}"
        f" · matched {decision.get('matched')}"
    )
    lines.append(decision_line)
    for reason in data.get("reasons") or []:
        lines.append(f"  reason: {reason}")
    return "\n".join(lines)


@log_app.command("compare")
def log_compare(
    trade_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compare the proposed signal with the realized outcome for one trade."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).journal_compare(trade_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_compare(data))


@log_app.command("export")
def log_export(
    format: Annotated[
        str,
        typer.Option("--format", help="Export format: csv or json."),
    ] = "csv",
    period: Annotated[
        str | None,
        typer.Option("--period", help="Window: Nd days, Nw weeks, Nm months (30 days)."),
    ] = None,
    symbol: Annotated[str | None, typer.Option("--symbol", help="Filter by symbol.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Write the journal under the data directory (CSV formulas neutralized)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).journal_export(
            format=format, period=period, symbol=symbol.upper() if symbol else None
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    typer.echo(
        f"EXPORTED {data.get('path', '-')} · {data.get('format', '-')}"
        f" · {data.get('rows', 0)} row(s) · {data.get('bytes', 0)} bytes"
    )


def _render_review(data: dict[str, Any]) -> str:
    lines = [
        ADVISORY_LABEL,
        f"REVIEW {data.get('trade_id', '-')} · status {data.get('status', '-')}"
        f" · decision {data.get('decision_id') or '-'}"
        f" · validation {data.get('validation_status') or '-'}"
        f" · confidence {data.get('confidence') if data.get('confidence') is not None else '-'}",
    ]
    if data.get("idempotent_replay"):
        lines.append("reused the linked review (no provider call)")
    if data.get("rationale"):
        lines.append(f"rationale: {data['rationale']}")
    for caveat in data.get("caveats") or []:
        lines.append(f"caveat: {caveat}")
    if data.get("reason"):
        lines.append(f"reason: {data['reason']}")
    return "\n".join(lines)


@log_app.command("review")
def log_review(
    trade_id: str,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Supersede the linked review with a new decision."),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="LLM provider name (defaults to the configured one)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Ask for an advisory LLM review of one closed trade (never executes)."""
    settings = _settings_or_exit(json_output)
    # A plain review reuses the linked decision for the same trade; a refresh
    # must use a fresh key so the server persists a new decision and retains it.
    # The provider is part of the key because it is part of the API request
    # hash: a plain call and a ``--provider`` call must not collide (HTTP 409).
    provider_slug = (provider or "").strip().lower() or "default"
    idempotency_key = uuid.uuid4().hex if refresh else f"review:{trade_id}:{provider_slug}"
    try:
        envelope = ApiClient.from_settings(settings).journal_review(
            trade_id,
            refresh=refresh,
            provider=provider,
            idempotency_key=idempotency_key,
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    typer.echo(_render_review(data))
