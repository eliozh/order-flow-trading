"""``oft risk show|set|validate``: read and persist the percentage-based caps (D4).

The caps are read from ``OFT_RISK_*`` defaults and overridden by the operator
row in ``system_state.risk_caps``. Loosening any cap requires ``--confirm`` and
always writes one hash-chained ``RISK_CAPS_UPDATED`` audit event.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.entry_service import RiskCapsError, effective_caps
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, Settings, load_settings
from oft.domain.risk import (
    DEFAULT_CAPS,
    RISK_CAPS_STATE_KEY,
    RiskCaps,
    caps_loosened,
    validate_caps,
)
from oft.domain.time import utc_timestamp
from oft.ports.store import AuditEventDraft

risk_app = typer.Typer(
    help="Risk caps: show, tighten, or (with --confirm) loosen the paper limits.",
    no_args_is_help=True,
    add_completion=False,
)


def _load() -> tuple[Settings, Any]:
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc
    return settings, build_container(settings, initialize=True)


def _emit(json_output: bool, data: dict[str, Any]) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    data,
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    typer.echo(f"risk caps · source {data['source']}")
    for name in data["caps"]:
        typer.echo(f"  {name:<28}{data['caps'][name]}")
    typer.echo(f"  defaults: {json.dumps(data['defaults'], sort_keys=True)}")


@risk_app.command("show")
def risk_show(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the effective caps, their source, and the documented defaults."""
    settings, container = _load()
    try:
        caps, source = effective_caps(settings, container.store)
    except RiskCapsError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    _emit(
        json_output,
        {"caps": caps.to_state(), "source": source, "defaults": DEFAULT_CAPS.to_state()},
    )


def _proposed_caps(current: RiskCaps, changes: dict[str, Any]) -> RiskCaps:
    return replace(current, **{name: value for name, value in changes.items() if value is not None})


@risk_app.command("set")
def risk_set(
    per_trade_pct: Annotated[float | None, typer.Option("--per-trade-pct")] = None,
    max_concurrent_positions: Annotated[
        int | None, typer.Option("--max-concurrent-positions")
    ] = None,
    daily_loss_pct: Annotated[float | None, typer.Option("--daily-loss-pct")] = None,
    max_leverage: Annotated[float | None, typer.Option("--max-leverage")] = None,
    max_notional_pct: Annotated[float | None, typer.Option("--max-notional-pct")] = None,
    max_slippage_bps: Annotated[float | None, typer.Option("--max-slippage-bps")] = None,
    max_spread_bps: Annotated[float | None, typer.Option("--max-spread-bps")] = None,
    stale_mark_seconds: Annotated[int | None, typer.Option("--stale-mark-seconds")] = None,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Required to loosen any cap (higher risk or longer stale)."),
    ] = False,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why the caps are being changed.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Persist new caps in one transaction, with a ``RISK_CAPS_UPDATED`` audit."""
    settings, container = _load()
    try:
        current, _source = effective_caps(settings, container.store)
    except RiskCapsError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    changes = {
        "per_trade_risk_pct": per_trade_pct,
        "max_concurrent_positions": max_concurrent_positions,
        "daily_loss_pct": daily_loss_pct,
        "max_leverage": max_leverage,
        "max_notional_pct": max_notional_pct,
        "max_slippage_bps": max_slippage_bps,
        "max_spread_bps": max_spread_bps,
        "stale_mark_seconds": stale_mark_seconds,
    }
    if all(value is None for value in changes.values()):
        emit_error(
            "VALIDATION_FAILED",
            "no risk cap was provided to set",
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    proposed = _proposed_caps(current, changes)
    errors = validate_caps(proposed)
    if errors:
        emit_error(
            "VALIDATION_FAILED",
            "invalid risk caps: " + "; ".join(errors),
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    loosened = caps_loosened(current, proposed)
    if loosened and not confirm:
        emit_error(
            "VALIDATION_FAILED",
            f"loosening {', '.join(loosened)} requires --confirm",
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    now = utc_timestamp(container.clock.now())
    audit = AuditEventDraft(
        event_type="RISK_CAPS_UPDATED",
        actor={"kind": "USER", "id": "cli"},
        subject={"kind": "SYSTEM", "id": RISK_CAPS_STATE_KEY},
        payload={
            "before": current.to_state(),
            "after": proposed.to_state(),
            "loosened": list(loosened),
            "reason": reason or ("loosening confirmed by operator" if loosened else ""),
        },
        created_at=now,
    )
    container.store.save_risk_caps(
        value=json.dumps(proposed.to_state(), sort_keys=True),
        updated_at=now,
        audit=audit,
    )
    _emit(
        json_output,
        {
            "caps": proposed.to_state(),
            "source": "persisted",
            "defaults": DEFAULT_CAPS.to_state(),
            "loosened": list(loosened),
        },
    )


@risk_app.command("validate")
def risk_validate(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Check the effective caps' invariants; never places an order."""
    settings, container = _load()
    try:
        caps, source = effective_caps(settings, container.store)
    except RiskCapsError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    errors = list(validate_caps(caps))
    status = "PASS" if not errors else "FAIL"
    data = {"status": status, "source": source, "caps": caps.to_state(), "errors": errors}
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    data,
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE" if not errors else "PARTIAL",
                ),
                indent=2,
            )
        )
    elif errors:
        typer.echo("FAIL")
        for error in errors:
            typer.echo(f"  - {error}")
    else:
        typer.echo(f"PASS · risk caps valid · source {source}")
    if errors:
        raise typer.Exit(code=1)


__all__ = ["risk_app"]
