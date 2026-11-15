"""Budget threshold and usage routes (D9; F10e usage over the same service)."""

from __future__ import annotations

from typing import Any, cast

import structlog
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.idempotency import idempotent_mutation
from oft.api.schemas.settings import ERROR_RESPONSES
from oft.application.budget_read_model import BUDGET_READ_ERRORS, BudgetThresholds
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.domain.time import utc_timestamp

LOGGER = structlog.get_logger(__name__)

router = APIRouter()

_PERIODS = ("day", "week", "month")

# A ledger/config read that fails in one of these ways degrades the usage
# display (N/A) instead of failing the whole budget read: the thresholds stay
# visible and editable while the operator recovers the ledger.
_USAGE_READ_ERRORS = BUDGET_READ_ERRORS


class BudgetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: str | None = None
    weekly_usd: str | None = None
    monthly_usd: str | None = None
    warn_pct: float | None = None


def _service(request: Request) -> BudgetService:
    container = request.app.state.container
    return BudgetService(store=container.store, settings=container.settings)


def _degraded_snapshot(thresholds: BudgetThresholds) -> dict[str, object]:
    """Thresholds stay readable; usage is explicitly unavailable, never zero."""
    return {
        "thresholds": thresholds.to_contract(),
        "usage_quality": "UNAVAILABLE",
        "budget_state": None,
        "periods": None,
        "projected_month_end_usd": None,
        "projection_basis": None,
        "last_alert": None,
    }


def _snapshot(service: BudgetService, at: str, thresholds: BudgetThresholds) -> dict[str, Any]:
    try:
        return service.snapshot(at, thresholds=thresholds)
    except _USAGE_READ_ERRORS as exc:
        LOGGER.warning("budget_usage_unavailable", error=str(exc))
        return _degraded_snapshot(thresholds)


def _envelope_quality(data: dict[str, Any]) -> str:
    """Map the payload's usage quality onto the shared envelope enum.

    ``UNAVAILABLE`` usage is a missing required input, which the envelope
    contract spells ``MISSING`` (the payload keeps its own ``usage_quality``).
    """
    return "COMPLETE" if data.get("usage_quality") == "COMPLETE" else "MISSING"


@router.get("/api/v1/budget", responses=ERROR_RESPONSES)
def show_budget(request: Request) -> dict[str, Any]:
    """Thresholds plus the current day/week/month usage (never a hard stop).

    A ledger read failure degrades only the usage block: ``thresholds`` is
    always returned, ``usage_quality`` becomes ``UNAVAILABLE``, and the usage
    fields are ``null``. If the thresholds themselves cannot be read, the whole
    read fails with 503 so the client can offer a retry.
    """
    container = request.app.state.container
    service = _service(request)
    at = utc_timestamp(container.clock.now())
    try:
        thresholds = service.thresholds()
    except _USAGE_READ_ERRORS as exc:
        LOGGER.warning("budget_thresholds_unavailable", error=str(exc))
        raise ApiError("STORAGE_UNAVAILABLE") from exc
    data = _snapshot(service, at, thresholds)
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=_envelope_quality(data),
    )


@router.put(
    "/api/v1/budget",
    response_model=None,
    responses=ERROR_RESPONSES,
)
def update_budget(
    payload: BudgetUpdateRequest,
    request: Request,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Optional; when present the mutation is replayed for the same key "
            "and a different payload with that key is a 409."
        ),
        max_length=200,
    ),
) -> dict[str, Any] | JSONResponse:
    """Persist operator thresholds, auditing only a real change (alert-only).

    ``Idempotency-Key`` is optional: without one the mutation runs once and
    returns the same envelope; with one, a replay returns the stored response
    and a different payload for the same key is a 409.
    """
    if (
        payload.daily_usd is None
        and payload.weekly_usd is None
        and payload.monthly_usd is None
        and payload.warn_pct is None
    ):
        raise ApiError("VALIDATION_FAILED", "provide at least one budget threshold")

    def mutate() -> dict[str, Any]:
        container = request.app.state.container
        service = _service(request)
        at = utc_timestamp(container.clock.now())
        try:
            thresholds, changed = service.update_thresholds(
                daily_usd=payload.daily_usd,
                weekly_usd=payload.weekly_usd,
                monthly_usd=payload.monthly_usd,
                warn_pct=payload.warn_pct,
                updated_at=at,
                actor="web",
            )
        except BudgetConfigError as exc:
            raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
        return {**_snapshot(service, at, thresholds), "changed": changed}

    presented = (idempotency_key or "").strip()
    if not presented:
        data = mutate()
        return success_envelope(
            data,
            request_id=new_request_id(),
            provenance="CALC",
            data_quality=_envelope_quality(data),
        )
    return cast(
        dict[str, Any] | JSONResponse,
        idempotent_mutation(
            request,
            presented_key=presented,
            payload=payload,
            status=200,
            mutate=mutate,
            data_quality=_envelope_quality,
        ),
    )


@router.get("/api/v1/budget/usage")
def budget_usage(
    request: Request,
    period: str = Query(default="day", pattern="^(day|week|month)$"),
) -> dict[str, Any]:
    container = request.app.state.container
    try:
        usage = _service(request).usage(period, at=utc_timestamp(container.clock.now()))
    except BudgetConfigError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message) from exc
    return success_envelope(
        usage,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


__all__ = ["router"]
