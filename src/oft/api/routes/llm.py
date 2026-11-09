"""LLM provider, model, capability-probe, and usage routes (D9)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.application.llm_orchestrator import (
    REAL_PROVIDERS,
    CapabilityProbeService,
    LLMConfigurationError,
    capability_status,
    model_catalog,
    provider_catalog,
)
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole

router = APIRouter()

_PERIOD_PATTERN = r"^(day|week|month)$"


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    role: str = "ANALYSIS"


def _probe_service(request: Request) -> CapabilityProbeService:
    container = request.app.state.container
    return CapabilityProbeService(
        settings=container.settings,
        store=container.store,
        clock=container.clock,
        runtime=container.llm_runtime,
    )


@router.get("/api/v1/llm/providers")
def list_providers(request: Request) -> dict[str, Any]:
    """Configured providers, role models, and the capability chip; never a key."""
    container = request.app.state.container
    items = provider_catalog(container.settings, container.store)
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/llm/models")
def list_models(
    request: Request,
    provider: str = Query(pattern="^(openai|anthropic|deepseek)$"),
) -> dict[str, Any]:
    """The versioned price catalog plus role-configured models (unpriced)."""
    container = request.app.state.container
    items = model_catalog(container.settings, provider)
    return success_envelope(
        {"provider": provider, "items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.post("/api/v1/llm/test")
def probe_provider(payload: ProbeRequest, request: Request) -> dict[str, Any]:
    """Run one capability probe and return the recorded row."""
    try:
        role = LLMRole(payload.role.strip().upper())
    except ValueError as exc:
        raise ApiError("VALIDATION_FAILED", f"unknown role {payload.role!r}") from exc
    try:
        record = _probe_service(request).run(payload.provider, role=role)
    except LLMConfigurationError as exc:
        raise ApiError(exc.code, exc.message) from exc
    if record.status == "FAILED" and record.error_code == "LLM_SCHEMA_UNSUPPORTED":
        raise ApiError("LLM_SCHEMA_UNSUPPORTED", record.notes)
    capability = {
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
    return success_envelope(
        capability,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if record.status == "OK" else "PARTIAL",
    )


@router.get("/api/v1/llm/usage")
def llm_usage(
    request: Request,
    period: str = Query(default="day", pattern=_PERIOD_PATTERN),
) -> dict[str, Any]:
    """Spend and unpriced counts for the requested window."""
    container = request.app.state.container
    service = BudgetService(store=container.store, settings=container.settings)
    at = utc_timestamp(container.clock.now())
    try:
        usage = service.usage(period, at=at)
    except BudgetConfigError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message) from exc
    return success_envelope(
        usage,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


__all__ = ["REAL_PROVIDERS", "router"]
