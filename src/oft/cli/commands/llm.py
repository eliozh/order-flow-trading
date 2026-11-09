"""``oft llm providers|models|test|usage`` (F12; D8/D9)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.application.llm_orchestrator import (
    REAL_PROVIDERS,
    CapabilityProbeService,
    LLMConfigurationError,
    capability_status,
    model_catalog,
    provider_catalog,
)
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole

llm_app = typer.Typer(
    help="LLM providers, models, capability probes, and usage.",
    no_args_is_help=True,
    add_completion=False,
)

LOGGER = structlog.get_logger(__name__)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit_json(data: dict[str, Any], *, data_quality: str = "COMPLETE") -> None:
    typer.echo(
        json.dumps(
            success_envelope(
                data,
                request_id=new_request_id(),
                provenance="CALC",
                data_quality=data_quality,
            ),
            indent=2,
        )
    )


def _chip(item: dict[str, Any]) -> str:
    capability = item.get("capability")
    if capability is None:
        return "UNCHECKED"
    return str(capability.get("status", "UNCHECKED"))


def _render_providers(items: list[dict[str, Any]]) -> str:
    lines = [f"{'PROVIDER':<12}{'KEYS':<8}{'CHIP':<12}ROLES"]
    for item in items:
        roles = ", ".join(
            f"{role}={model or '-'}" for role, model in (item.get("roles") or {}).items()
        )
        lines.append(
            f"{item['provider']:<12}{'set' if item['configured'] else 'unset':<8}"
            f"{_chip(item):<12}{roles}"
        )
    return "\n".join(lines)


def _render_models(provider: str, items: list[dict[str, Any]]) -> str:
    lines = [f"MODELS · {provider}", ""]
    width = max((len(str(item["model"])) for item in items), default=8)
    lines.append(f"{'MODEL':<{width}}  {'STATUS':<10}{'IN/MTok':<10}{'OUT/MTok':<10}VERSION")
    for item in items:
        status = "unpriced" if item["unpriced"] else "priced"
        lines.append(
            f"{str(item['model']):<{width}}  {status:<10}{item['input_per_mtok']:<10}"
            f"{item['output_per_mtok']:<10}{item['price_version']}"
        )
    return "\n".join(lines)


def _render_usage(usage: dict[str, Any]) -> str:
    rows = [
        ("Period", f"{usage['period']} ({usage['period_key']})"),
        ("Spend (USD)", str(usage["spend_usd"])),
        ("Calls", str(usage["call_count"])),
        ("Unpriced calls", str(usage["unpriced_count"])),
        ("State", str(usage["budget_state"])),
        (
            "Thresholds",
            f"day {usage['thresholds']['daily_usd']} / week "
            f"{usage['thresholds']['weekly_usd']} / month "
            f"{usage['thresholds']['monthly_usd']} (warn {usage['thresholds']['warn_pct']}%)",
        ),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{width}}  {value}" for label, value in rows]
    if usage.get("by_provider"):
        lines.append("")
        lines.append("BY PROVIDER")
        for row in usage["by_provider"]:
            lines.append(
                f"  {row['provider']:<12}{row['spend_usd']:<16}"
                f"{row['call_count']} calls · {row['unpriced_count']} unpriced"
            )
    return "\n".join(lines)


@llm_app.command("providers")
def providers_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List providers, key presence, role models, and the capability chip."""
    settings = _settings_or_exit(json_output)
    container = build_container(settings)
    items = provider_catalog(settings, container.store)
    if json_output:
        _emit_json({"items": items, "total": len(items)})
        return
    typer.echo(_render_providers(items))


@llm_app.command("models")
def models_command(
    provider: Annotated[str, typer.Option("--provider", help="openai|anthropic|deepseek")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List the price catalog plus role-configured models for one provider."""
    name = provider.strip().lower()
    if name not in REAL_PROVIDERS:
        emit_error(
            "VALIDATION_FAILED",
            f"unknown provider {provider!r}; expected one of {', '.join(REAL_PROVIDERS)}",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(json_output)
    items = model_catalog(settings, name)
    if json_output:
        _emit_json(
            {"provider": name, "items": items, "total": len(items)},
            data_quality="COMPLETE" if items else "MISSING",
        )
        return
    typer.echo(_render_models(name, items))


@llm_app.command("test")
def test_command(
    provider: Annotated[str, typer.Option("--provider", help="openai|anthropic|deepseek")],
    role: Annotated[str, typer.Option("--role", help="ANALYSIS|ENTRY_RECHECK|...")] = "ANALYSIS",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Run one minimal structured probe and record the capability result."""
    try:
        resolved_role = LLMRole(role.strip().upper())
    except ValueError:
        emit_error("VALIDATION_FAILED", f"unknown role {role!r}", json_output=json_output)
        raise typer.Exit(code=2) from None
    settings = _settings_or_exit(json_output)
    container = build_container(settings, initialize=True)
    service = CapabilityProbeService(
        settings=settings,
        store=container.store,
        clock=container.clock,
        runtime=container.llm_runtime,
    )
    try:
        record = service.run(provider, role=resolved_role)
    except LLMConfigurationError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    payload = {
        "provider": record.provider,
        "model": record.model,
        "status": capability_status(record),
        "strict_schema": record.strict_schema,
        "degraded": record.degraded,
        "max_output_tokens": record.max_output_tokens,
        "error_code": record.error_code,
        "notes": record.notes,
        "latency_ms": record.latency_ms,
        "checked_at": record.checked_at,
    }
    if json_output:
        _emit_json(payload, data_quality="COMPLETE" if record.status == "OK" else "PARTIAL")
        return
    typer.echo(
        f"{record.provider}  {record.model}  {capability_status(record)}  "
        f"{record.notes or '-'}  ({record.latency_ms} ms)"
    )


@llm_app.command("usage")
def usage_command(
    period: Annotated[str, typer.Option("--period", help="day|week|month")] = "day",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show spend and unpriced counts for one budget window."""
    settings = _settings_or_exit(json_output)
    container = build_container(settings)
    service = BudgetService(store=container.store, settings=settings)
    at = utc_timestamp(container.clock.now())
    try:
        usage = service.usage(period.strip().lower(), at=at)
    except BudgetConfigError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 - surface the real code, never mask it
        code = getattr(exc, "code", "INTERNAL_ERROR") or "INTERNAL_ERROR"
        LOGGER.error(
            "llm_usage_failed",
            period=period,
            error=type(exc).__name__,
            code=code,
        )
        emit_error(code, str(exc), json_output=json_output)
        raise typer.Exit(code=exit_code_for(code)) from exc
    if json_output:
        _emit_json(usage)
        return
    typer.echo(_render_usage(usage))


__all__ = ["llm_app"]
