"""``oft budget show|set|usage`` (F12; D9)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, load_settings
from oft.domain.time import utc_timestamp

budget_app = typer.Typer(
    help="Budget thresholds and day/week/month usage.",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit_json(data: dict[str, Any]) -> None:
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


def _show_payload(service: BudgetService, at: str) -> dict[str, Any]:
    return service.snapshot(at)


def _render_show(payload: dict[str, Any]) -> str:
    thresholds = payload["thresholds"]
    projection = payload["projection_basis"]
    lines = [
        f"Budget state      {payload['budget_state']}",
        f"Daily threshold   {thresholds['daily_usd']} USD (warn {thresholds['warn_pct']}%)",
        f"Weekly threshold  {thresholds['weekly_usd']} USD",
        f"Monthly threshold {thresholds['monthly_usd']} USD",
        f"Projected month-end {payload['projected_month_end_usd']} USD "
        f"(spend {projection['month_spend_usd']} USD, elapsed fraction "
        f"{projection['elapsed_fraction']:.6f} as of {projection['as_of']})",
    ]
    last_alert = payload["last_alert"]
    if last_alert is None:
        lines.append("Last alert        none")
    else:
        lines.append(
            f"Last alert        {last_alert['occurred_at']} {last_alert['period']} "
            f"{last_alert['state']} {last_alert['spend_usd']}/{last_alert['threshold_usd']} "
            f"USD ({last_alert['provider']} {last_alert['model']})"
        )
    lines.append("")
    lines.append(f"{'PERIOD':<10}{'KEY':<14}{'SPEND USD':<16}{'CALLS':<8}{'UNPRICED':<10}STATE")
    for period, window in payload["periods"].items():
        lines.append(
            f"{period:<10}{window['period_key']:<14}{window['spend_usd']:<16}"
            f"{window['call_count']:<8}{window['unpriced_count']:<10}{window['budget_state']}"
        )
    return "\n".join(lines)


@budget_app.command("show")
def show_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show thresholds and the current day/week/month spend and state."""
    settings = _settings_or_exit(json_output)
    container = build_container(settings)
    service = BudgetService(store=container.store, settings=settings)
    payload = _show_payload(service, utc_timestamp(container.clock.now()))
    if json_output:
        _emit_json(payload)
        return
    typer.echo(_render_show(payload))


@budget_app.command("set")
def set_command(
    daily: Annotated[str | None, typer.Option("--daily", help="Daily USD threshold.")] = None,
    weekly: Annotated[str | None, typer.Option("--weekly", help="Weekly USD threshold.")] = None,
    monthly: Annotated[str | None, typer.Option("--monthly", help="Monthly USD threshold.")] = None,
    warn_pct: Annotated[
        float | None, typer.Option("--warn-pct", help="Warn percentage (0, 100].")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Persist threshold overrides; at least one value is required."""
    if all(value is None for value in (daily, weekly, monthly, warn_pct)):
        emit_error(
            "VALIDATION_FAILED",
            "provide at least one of --daily, --weekly, --monthly, --warn-pct",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(json_output)
    container = build_container(settings, initialize=True)
    service = BudgetService(store=container.store, settings=settings)
    try:
        thresholds = service.set_thresholds(
            daily_usd=daily,
            weekly_usd=weekly,
            monthly_usd=monthly,
            warn_pct=warn_pct,
            updated_at=utc_timestamp(container.clock.now()),
        )
    except BudgetConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    if json_output:
        _emit_json({"thresholds": thresholds.to_contract()})
        return
    typer.echo(
        f"Budget thresholds updated: day {thresholds.daily_usd} / "
        f"week {thresholds.weekly_usd} / month {thresholds.monthly_usd} "
        f"(warn {thresholds.warn_pct}%)"
    )


@budget_app.command("usage")
def usage_command(
    period: Annotated[str, typer.Option("--period", help="day|week|month")] = "day",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one window's spend, call count, and unpriced count."""
    settings = _settings_or_exit(json_output)
    container = build_container(settings)
    service = BudgetService(store=container.store, settings=settings)
    try:
        usage = service.usage(period.strip().lower(), at=utc_timestamp(container.clock.now()))
    except BudgetConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    if json_output:
        _emit_json(usage)
        return
    rows = [
        ("Period", f"{usage['period']} ({usage['period_key']})"),
        ("Spend (USD)", str(usage["spend_usd"])),
        ("Calls", str(usage["call_count"])),
        ("Unpriced calls", str(usage["unpriced_count"])),
        ("State", str(usage["budget_state"])),
    ]
    width = max(len(label) for label, _ in rows)
    typer.echo("\n".join(f"{label:<{width}}  {value}" for label, value in rows))


__all__ = ["budget_app"]
