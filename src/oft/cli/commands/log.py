"""``oft log list|show``: the closed-trade journal (read-only)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

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
    """Show one closed trade and its data-quality reasons."""
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
