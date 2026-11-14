"""``oft pos list|show|history|monitor|manage``: the position ledger and management."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

pos_app = typer.Typer(
    help=(
        "Position ledger, accounting history, the deterministic protective-exit tick, "
        "and the LLM management decision (paper only; no live path)."
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
    """Show one position and its management history."""
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


def _render_history(data: dict[str, Any]) -> str:
    lines = [
        f"POSITION {data.get('position_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · {data.get('state', '-')}",
        f"size {data.get('size', '-')} · entry {data.get('avg_entry_price', '-')}",
    ]
    pnl = data.get("pnl") or {}
    lines.append(f"pnl basis {pnl.get('basis', '-')}")
    lines.append(
        f"gross {pnl.get('gross_pnl_usd', '-')} · fees {pnl.get('fees_usd', '-')}"
        f" · funding {pnl.get('funding_usd', '-')} · net {pnl.get('net_pnl_usd', '-')}"
    )
    lines.append(
        f"r_multiple {pnl.get('r_multiple')} · return {pnl.get('return_pct')}"
        f" · mae {pnl.get('mae_usd', '-')} · mfe {pnl.get('mfe_usd', '-')}"
    )
    tags = pnl.get("outcome_tags") or []
    if tags:
        lines.append("outcome " + ", ".join(str(tag) for tag in tags))
    funding = data.get("funding") or []
    lines.append("FUNDING")
    if not funding:
        lines.append("  (no recorded settlement in the holding window)")
    for item in funding:
        status = (
            "accrued"
            if item.get("accrued")
            else f"not accrued: {item.get('reason') or 'reason unavailable'}"
        )
        lines.append(
            f"  {item.get('settlement_time', '-')} · rate {item.get('rate', '-')}"
            f" · mark {item.get('mark_price', '-')} · amount {item.get('amount_usd', '-')}"
            f" · {status}"
        )
    funding_reasons = data.get("funding_reasons") or []
    if funding_reasons:
        lines.append("FUNDING QUALITY")
        for reason in funding_reasons:
            lines.append(f"  {reason}")
    events = data.get("events") or []
    lines.append("TIMELINE")
    if not events:
        lines.append("  (no management events)")
    for event in events:
        lines.append(
            f"  {event.get('seq', '-')} · {event.get('action', '-')} · {event.get('at', '-')}"
            f" · order {event.get('order_id') or '-'} · {event.get('note', '')}"
        )
    return "\n".join(lines)


@pos_app.command("history")
def pos_history(
    position_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the accounting timeline: events, funding accruals, and PnL terms."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).position_history(position_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Position {position_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_history(data))


@pos_app.command("monitor")
def pos_monitor(
    position_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Accrue recorded funding, then run one stop-touch tick against the daemon."""
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


def _render_management(data: dict[str, Any]) -> str:
    lines = [
        f"MANAGE {data.get('position_id', '-')} · {data.get('action', '-')}"
        f" · {data.get('status', '-')}",
        f"decision {data.get('decision_id', '-')} · executed {data.get('executed')}",
    ]
    if data.get("reason"):
        lines.append(f"  reason: {data['reason']}")
    risk = data.get("risk") or {}
    if risk:
        lines.append(
            f"risk {risk.get('verdict', '-')} · cap {risk.get('binding_cap', '-')}"
            f" · allowed {((risk.get('allowed') or {}).get('quantity')) or '-'}"
        )
        stale = risk.get("stale_inputs") or []
        if stale:
            lines.append("  stale: " + ", ".join(str(name) for name in stale))
    llm = data.get("llm") or {}
    provenance = llm.get("provenance") or {}
    if provenance:
        cost = provenance.get("cost") or {}
        lines.append(
            f"llm {provenance.get('provider', '-')}/{provenance.get('model', '-')}"
            f" · prompt v{provenance.get('prompt_version', '-')}"
            f" · cost {cost.get('amount_usd', '-')} {cost.get('currency', 'USD')}"
        )
    order = data.get("order") or {}
    if order:
        lines.append(
            f"  order {order.get('order_id', '-')} · {order.get('status', '-')}"
            f" · {order.get('side', '-')} {order.get('quantity', '-')}"
        )
    fill = data.get("fill") or {}
    if fill:
        lines.append(
            f"  fill {fill.get('fill_id', '-')} · price {fill.get('price', '-')}"
            f" · qty {fill.get('quantity', '-')}"
        )
    trade = data.get("trade_log") or {}
    if trade:
        lines.append(f"  trade {trade.get('trade_id', '-')} · net {trade.get('net_pnl_usd', '-')}")
    if data.get("idempotent_replay"):
        lines.append("  (idempotent replay: no new rows were created)")
    return "\n".join(lines)


@pos_app.command("manage")
def pos_manage(
    position_id: str,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Reuse a key to replay the same manage call."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="LLM provider override for the management role."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Obtain a validated HOLD/PARTIAL_CLOSE/FULL_CLOSE/ADD decision and execute it."""
    settings = _settings_or_exit(json_output)
    key = idempotency_key or f"cli-manage-{uuid.uuid4().hex}"
    payload = {"provider": provider} if provider else {}
    try:
        envelope = ApiClient.from_settings(settings).manage_position(
            position_id, payload, idempotency_key=key
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_management(data))
