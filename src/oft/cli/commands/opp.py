"""``oft opp list|show|timeline|conditions|evaluate|export|expire-due``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.opportunity_service import (
    OpportunityService,
    opportunity_summary,
)
from oft.bootstrap import build_container
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
        typer.Option(
            "--state",
            help="open | triggered | expired | invalidated (case-insensitive).",
        ),
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
        if condition.get("unavailable_reason"):
            lines.append(f"         reason: {condition['unavailable_reason']}")
    invalidation = data.get("invalidation_conditions") or []
    if invalidation:
        lines.append("INVALIDATION")
        for condition in invalidation:
            params = json.dumps(condition.get("params", {}), sort_keys=True)
            lines.append(
                f"  {condition.get('condition_id', '-')} {condition.get('type', '-')}"
                f" evaluable={condition.get('evaluable')} params={params}"
            )
            if condition.get("unavailable_reason"):
                lines.append(f"         reason: {condition['unavailable_reason']}")
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


def _conditions_envelope(opportunity_id: str, json_output: bool) -> dict[str, Any]:
    settings = _settings_or_exit(json_output)
    try:
        return ApiClient.from_settings(settings).get_opportunity_conditions(opportunity_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc


@opp_app.command("conditions")
def opp_conditions(
    opportunity_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List every persisted trigger and invalidation condition."""
    envelope = _conditions_envelope(opportunity_id, json_output)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    typer.echo(f"CONDITIONS {data.get('opportunity_id', opportunity_id)}")
    for condition in data.get("conditions") or []:
        params = json.dumps(condition.get("params", {}), sort_keys=True)
        typer.echo(
            f"  {condition.get('condition_id', '-'):<6}"
            f"{condition.get('type', '-'):<30}"
            f"evaluable={condition.get('evaluable')} params={params}"
        )
        if condition.get("unavailable_reason"):
            typer.echo(f"         reason: {condition['unavailable_reason']}")


@opp_app.command("evaluate")
def opp_evaluate(
    opportunity_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Display-evaluate every condition against the recorded snapshot."""
    envelope = _conditions_envelope(opportunity_id, json_output)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    typer.echo(
        f"EVALUATION {data.get('opportunity_id', opportunity_id)}"
        f" · would_trigger={data.get('would_trigger')}"
        f" · quality={data.get('data_quality', '-')}"
        f" · as_of={data.get('as_of', '-')}"
    )
    for evaluation in data.get("evaluations") or []:
        typer.echo(
            f"  {evaluation.get('condition_id', '-'):<6}"
            f"{evaluation.get('type', '-'):<30}"
            f"met={str(evaluation.get('met')).upper():<5}"
            f"evaluable={str(evaluation.get('evaluable')).upper():<5}"
            f"observed={evaluation.get('observed_value') or '-'}"
            f" threshold={evaluation.get('threshold') or '-'}"
        )
        if evaluation.get("note"):
            typer.echo(f"         {evaluation['note']}")


@opp_app.command("export")
def opp_export(
    opportunity_id: str,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the contract envelope to a file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Export the opportunity contract envelope (read-only)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).export_opportunity(opportunity_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    text = json.dumps(envelope, indent=2)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"Wrote {out}")
        return
    typer.echo(text)


@opp_app.command("expire-due")
def opp_expire_due(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Expire every OPEN opportunity whose ``expires_at`` has passed (code-only)."""
    settings = _settings_or_exit(json_output)
    container = build_container(settings)
    service = OpportunityService(store=container.store, clock=container.clock)
    expired = service.expire_due()
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    {"items": [opportunity_summary(item) for item in expired]},
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE" if expired else "MISSING",
                ),
                indent=2,
            )
        )
        return
    if not expired:
        typer.echo("No opportunities expired.")
        return
    for opportunity in expired:
        typer.echo(f"{opportunity.opportunity_id} → EXPIRED · expires_at reached")
