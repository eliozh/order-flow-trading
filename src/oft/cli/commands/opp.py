"""``oft opp list|show|timeline``: the structured opportunity ledger."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

opp_app = typer.Typer(
    help="Opportunity ledger commands (read-only; only code changes state).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


@opp_app.command("list")
def opp_list(
    state: Annotated[
        str | None,
        typer.Option("--state", help="OPEN | TRIGGERED | EXPIRED | INVALIDATED."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List opportunities, optionally filtered by state."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_opportunities(
            state=state.upper() if state else None, limit=limit
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    items = data.get("items") or []
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    if not items:
        typer.echo("No opportunities" + (f" in state {state.upper()}" if state else "") + ".")
        return
    typer.echo(f"{'OPPORTUNITY':<40}{'SYMBOL':<12}{'SIDE':<7}{'STATE':<12}CONF")
    for item in items:
        typer.echo(
            f"{str(item.get('opportunity_id', '-')):<40}"
            f"{str(item.get('symbol', '-')):<12}"
            f"{str(item.get('side', '-')):<7}"
            f"{str(item.get('state', '-')):<12}"
            f"{item.get('confidence', '-')}"
        )


def _render_opportunity(data: dict[str, Any]) -> str:
    zone = data.get("zone") or {}
    source = data.get("source") or {}
    lines = [
        f"OPPORTUNITY {data.get('opportunity_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · {data.get('state', '-')}",
        f"confidence {data.get('confidence', '-')} · entry {data.get('entry_preference', '-')}"
        f" · expires {data.get('expires_at', '-')}",
        f"zone {zone.get('low', '-')} → {zone.get('high', '-')}",
        f"source scan {source.get('scan_id', '-')} · report {source.get('report_id', '-')}"
        f" · decision {source.get('decision_id', '-')}",
        "",
        "CONDITIONS",
    ]
    for condition in data.get("trigger_conditions") or []:
        params = json.dumps(condition.get("params", {}), sort_keys=True)
        lines.append(
            f"  {condition.get('condition_id', '-')} {condition.get('type', '-')}"
            f" evaluable={condition.get('evaluable')} params={params}"
        )
    invalidation = data.get("invalidation_conditions") or []
    if invalidation:
        lines.append("INVALIDATION")
        for condition in invalidation:
            lines.append(f"  {condition.get('condition_id', '-')} {condition.get('type', '-')}")
    lines.append("")
    lines.append("TIMELINE")
    for entry in data.get("state_history") or []:
        lines.append(
            f"  {entry.get('from_state')} → {entry.get('to_state')}"
            f" · {entry.get('at', '-')} · {entry.get('reason_code', '-')}"
            f" · {entry.get('note', '')}"
        )
    return "\n".join(lines)


@opp_app.command("show")
def opp_show(
    opportunity_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one opportunity, its conditions, provenance, and timeline."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_opportunity(opportunity_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error(
            "NOT_FOUND",
            f"Opportunity {opportunity_id} does not exist.",
            json_output=json_output,
        )
        raise typer.Exit(code=1)
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_opportunity(data))


@opp_app.command("timeline")
def opp_timeline(
    opportunity_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the append-only state timeline."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_opportunity_timeline(opportunity_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = (envelope.get("data") or {}).get("items") or []
    if not items:
        typer.echo("No state history.")
        return
    for entry in items:
        typer.echo(
            f"{entry.get('from_state')} → {entry.get('to_state')}"
            f" · {entry.get('at', '-')} · {entry.get('reason_code', '-')}"
        )
