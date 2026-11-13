"""``oft pos list|show|monitor``: the position ledger and the manual tick."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

pos_app = typer.Typer(
    help=(
        "Position ledger and the deterministic protective-exit tick "
        "(no target, partial, add, or live path)."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _render_position(data: dict[str, Any]) -> str:
    lines = [
        f"POSITION {data.get('position_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · {data.get('state', '-')}",
        f"size {data.get('size', '-')} · entry {data.get('avg_entry_price', '-')}"
        f" · mark {data.get('mark_price', '-')} · stop {data.get('stop_price', '-')}",
        f"fees {data.get('fees_usd', '-')} · funding {data.get('funding_usd', '-')}"
        f" · realized {data.get('realized_pnl_usd', '-')}",
        f"opened {data.get('opened_at', '-')} · updated {data.get('updated_at', '-')}"
        f" · closed {data.get('closed_at', '-')} · exit {data.get('exit_price', '-')}",
    ]
    history = data.get("management_history") or []
    if history:
        lines.append("MANAGEMENT")
        for entry in history:
            lines.append(
                f"  {entry.get('action', '-')} · {entry.get('at', '-')}"
                f" · {entry.get('decision_id', '-')}"
                f" · order {entry.get('executed_order_id') or '-'}"
                f" · {entry.get('note', '')}"
            )
    return "\n".join(lines)


@pos_app.command("list")
def pos_list(
    state: Annotated[
        str | None,
        typer.Option("--state", help="open | closed | liquidated | reconciling."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List positions, optionally filtered by state."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_positions(
            state=state.upper() if state else None, limit=limit
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = (envelope.get("data") or {}).get("items") or []
    if not items:
        typer.echo("No positions" + (f" in state {state.upper()}" if state else "") + ".")
        return
    typer.echo(
        f"{'POSITION':<24}{'SYMBOL':<10}{'SIDE':<7}{'STATE':<12}"
        f"{'SIZE':<12}{'ENTRY':<14}{'MARK':<14}REALIZED"
    )
    for item in items:
        typer.echo(
            f"{str(item.get('position_id', '-')):<24}"
            f"{str(item.get('symbol', '-')):<10}"
            f"{str(item.get('side', '-')):<7}"
            f"{str(item.get('state', '-')):<12}"
            f"{str(item.get('size', '-')):<12}"
            f"{str(item.get('avg_entry_price', '-')):<14}"
            f"{str(item.get('mark_price', '-')):<14}"
            f"{item.get('realized_pnl_usd', '-')}"
        )


@pos_app.command("show")
def pos_show(
    position_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one position, its fills, and its management history."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_position(position_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Position {position_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_position(data))


@pos_app.command("monitor")
def pos_monitor(
    position_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Run one stop-touch tick against the daemon (idempotent)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).monitor_position(position_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    typer.echo(
        f"{data.get('position_id', position_id)} · {data.get('status', '-')}"
        f" · {data.get('reason_code', '-')}"
    )
    if data.get("reason"):
        typer.echo(f"  reason: {data['reason']}")
    trade = data.get("trade_log") or {}
    if trade:
        typer.echo(
            f"  trade {trade.get('trade_id', '-')} · net {trade.get('net_pnl_usd', '-')}"
            f" · fees {trade.get('fees_usd', '-')} · funding {trade.get('funding_usd', '-')}"
        )
    order = data.get("order") or {}
    if order:
        typer.echo(f"  order {order.get('order_id', '-')} · {order.get('status', '-')}")
    if data.get("idempotent_replay"):
        typer.echo("  (idempotent replay: no new rows were created)")
