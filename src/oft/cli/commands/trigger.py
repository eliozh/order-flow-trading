"""``oft trigger list|show|evaluate|accept|reject`` (paper only)."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

trigger_app = typer.Typer(
    help="Trigger queue: code-decided triggers and human paper accept/reject.",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


@trigger_app.command("list")
def trigger_list(
    opportunity_id: Annotated[
        str | None, typer.Option("--opportunity", help="Filter to one opportunity.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by trigger status.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List trigger events (newest first)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).list_triggers(
            opportunity_id=opportunity_id,
            status=status,
            limit=limit,
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    items = (envelope.get("data") or {}).get("items") or []
    if not items:
        typer.echo("No triggers yet. Run: oft trigger evaluate <opportunity_id>")
        return
    typer.echo(f"{'TRIGGER':<40}{'SYMBOL':<12}{'SIDE':<7}{'STATUS':<16}ORDER")
    for item in items:
        typer.echo(
            f"{str(item.get('trigger_id', '-')):<40}"
            f"{str(item.get('symbol', '-')):<12}"
            f"{str(item.get('side', '-')):<7}"
            f"{str(item.get('status', '-')):<16}"
            f"{item.get('created_order_id') or '-'}"
        )


@trigger_app.command("show")
def trigger_show(
    trigger_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one trigger with condition results and snapshot provenance."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).get_trigger(trigger_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    snapshot = data.get("snapshot") or {}
    snapshot_id = snapshot.get("market_snapshot_id", "-")
    snapshot_digest = snapshot.get("snapshot_hash", "-")
    order_id = data.get("created_order_id") or "-"
    lines = [
        f"TRIGGER {data.get('trigger_id', '-')} · {data.get('symbol', '-')}"
        f" · {data.get('side', '-')} · {data.get('status', '-')}",
        f"source {data.get('trigger_source', '-')} · triggered {data.get('triggered_at', '-')}",
        f"snapshot {snapshot_id} · hash {snapshot_digest}",
        f"idempotency {data.get('idempotency_key', '-')} · order {order_id}",
        "",
        "CONDITION RESULTS",
    ]
    for result in data.get("condition_results") or []:
        lines.append(
            f"  {result.get('condition_id', '-')} {result.get('type', '-')} met={result.get('met')}"
            f" observed={result.get('observed_value')} threshold={result.get('threshold')}"
        )
    if data.get("rejection_reason"):
        lines.append(f"rejection {data['rejection_reason']}")
    return_value = "\n".join(lines)
    typer.echo(return_value)


@trigger_app.command("evaluate")
def trigger_evaluate(
    opportunity_id: Annotated[str | None, typer.Argument(help="Opportunity to evaluate.")] = None,
    evaluate_all: Annotated[
        bool,
        typer.Option("--all", help="Evaluate every OPEN opportunity (the job path)."),
    ] = False,
    price: Annotated[
        str | None, typer.Option("--price", help="Override the snapshot reference price.")
    ] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Snapshot timestamp (UTC ISO-8601).")
    ] = None,
    snapshot_id: Annotated[
        str | None, typer.Option("--snapshot-id", help="Explicit snapshot identifier.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Evaluate one opportunity's conditions, or every OPEN opportunity with --all.

    Only code can trigger; the evaluator is the same one the 15 s job uses.
    """
    settings = _settings_or_exit(json_output)
    client = ApiClient.from_settings(settings)
    if evaluate_all and (price is not None or as_of is not None or snapshot_id is not None):
        emit_error(
            "VALIDATION_FAILED",
            "--all cannot be combined with --price, --as-of, or --snapshot-id",
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    if evaluate_all:
        try:
            envelope = client.evaluate_all_triggers(idempotency_key=f"cli-all-{uuid.uuid4().hex}")
        except ApiClientError as exc:
            emit_error(exc.code, exc.message, json_output=json_output)
            raise typer.Exit(code=exit_code_for(exc.code)) from exc
        if json_output:
            typer.echo(json.dumps(envelope, indent=2))
            return
        data = envelope.get("data") or {}
        counts_line = (
            "TRIGGER EVALUATE"
            f" open={data.get('open', 0)}"
            f" evaluated={data.get('evaluated', 0)}"
            f" triggered={data.get('triggered', 0)}"
            f" skipped={data.get('skipped', 0)}"
            f" stale={data.get('stale', 0)}"
            f" unavailable={data.get('unavailable', 0)}"
        )
        if data.get("open_total") and int(data["open_total"]) != int(data.get("open", 0)):
            counts_line += f" open_total={data['open_total']}"
        typer.echo(counts_line)
        if data.get("trigger_ids"):
            typer.echo("triggers " + " ".join(str(item) for item in data["trigger_ids"]))
        return
    if opportunity_id is None:
        emit_error(
            "VALIDATION_FAILED",
            "opportunity_id is required unless --all is used",
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    payload: dict[str, Any] = {"opportunity_id": opportunity_id}
    if price is not None:
        payload["price"] = price
    if as_of is not None:
        payload["as_of"] = as_of
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    try:
        envelope = client.evaluate_trigger(payload, idempotency_key=f"cli-{uuid.uuid4().hex}")
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    trigger = data.get("trigger")
    if trigger is None:
        evaluation = data.get("evaluation") or {}
        reasons = "; ".join(evaluation.get("reasons") or []) or "conditions not met"
        typer.echo(f"No trigger. {reasons}")
        return
    typer.echo(
        f"TRIGGER {trigger.get('trigger_id')} · {trigger.get('symbol')}"
        f" · {trigger.get('side')} · {trigger.get('status')}"
    )
    typer.echo(f"snapshot hash {(trigger.get('snapshot') or {}).get('snapshot_hash', '-')}")
    typer.echo(f"idempotency {trigger.get('idempotency_key', '-')}")
    typer.echo(f"Accept with: oft trigger accept {trigger.get('trigger_id')}")


def _render_entry(data: dict[str, Any]) -> str:
    order = data.get("order") or {}
    fill = data.get("fill") or {}
    position = data.get("position") or {}
    account = data.get("account") or {}
    verdict = data.get("risk_verdict") or {}
    lines = [
        f"ACCEPT {data.get('status', '-')} · decision {data.get('decision_id', '-')}",
        f"risk {verdict.get('verdict', '-')} binding {verdict.get('binding_cap', '-')}",
        f"order {order.get('order_id', '-')} {order.get('status', '-')}"
        f" · client {order.get('client_order_id', '-')}",
        f"fill {fill.get('fill_id', '-')} @ {fill.get('price', '-')}"
        f" qty {fill.get('quantity', '-')} fee {fill.get('fee', '-')}",
        f"position {position.get('position_id', '-')} {position.get('state', '-')}"
        f" size {position.get('size', '-')}",
        f"balance {account.get('balance', '-')} {account.get('currency', '')}".rstrip(),
    ]
    return "\n".join(lines)


@trigger_app.command("accept")
def trigger_accept(
    trigger_id: str,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Reuse a key to make the accept idempotent."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Accept a pending paper trigger through the risk gate and gateway."""
    settings = _settings_or_exit(json_output)
    key = idempotency_key or f"cli-accept-{uuid.uuid4().hex}"
    try:
        envelope = ApiClient.from_settings(settings).accept_trigger(trigger_id, idempotency_key=key)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    typer.echo(json.dumps(envelope, indent=2) if json_output else _render_entry(data))


@trigger_app.command("reject")
def trigger_reject(
    trigger_id: str,
    reason: Annotated[str, typer.Option("--reason", help="Why the entry is rejected.")],
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Reject a pending trigger; the opportunity becomes INVALIDATED and no order exists."""
    settings = _settings_or_exit(json_output)
    key = idempotency_key or f"cli-reject-{uuid.uuid4().hex}"
    try:
        envelope = ApiClient.from_settings(settings).reject_trigger(
            trigger_id, {"reason": reason}, idempotency_key=key
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(f"REJECTED {trigger_id} · {reason}")
