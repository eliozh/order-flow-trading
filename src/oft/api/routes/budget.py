"""Budget threshold and usage routes (D9; no Web settings screen this slice)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.domain.time import utc_timestamp

router = APIRouter()

_PERIODS = ("day", "week", "month")
_RANK = {"OK": 0, "WARN": 1, "EXCEEDED": 2}


class BudgetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: str | None = None
    weekly_usd: str | None = None
    monthly_usd: str | None = None
    warn_pct: float | None = None


def _service(request: Request) -> BudgetService:
    container = request.app.state.container
    return BudgetService(store=container.store, settings=container.settings)


@router.get("/api/v1/budget")
def show_budget(request: Request) -> dict[str, Any]:
    """Thresholds plus the current day/week/month state (never a hard stop)."""
    container = request.app.state.container
    service = _service(request)
    at = utc_timestamp(container.clock.now())
    windows = {period: service.usage(period, at=at) for period in _PERIODS}
    state = max(
        (window["budget_state"] for window in windows.values()),
        key=lambda value: _RANK[str(value)],
    )
    return success_envelope(
        {
            "thresholds": service.thresholds().to_contract(),
            "budget_state": state,
            "periods": {
                period: {
                    "period_key": window["period_key"],
                    "spend_usd": window["spend_usd"],
                    "call_count": window["call_count"],
                    "unpriced_count": window["unpriced_count"],
                    "budget_state": window["budget_state"],
                }
                for period, window in windows.items()
            },
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put("/api/v1/budget")
def update_budget(payload: BudgetUpdateRequest, request: Request) -> dict[str, Any]:
    """Persist operator thresholds and record the before/after audit event."""
    if (
        payload.daily_usd is None
        and payload.weekly_usd is None
        and payload.monthly_usd is None
        and payload.warn_pct is None
    ):
        raise ApiError("VALIDATION_FAILED", "provide at least one budget threshold")
    container = request.app.state.container
    try:
        thresholds = _service(request).set_thresholds(
            daily_usd=payload.daily_usd,
            weekly_usd=payload.weekly_usd,
            monthly_usd=payload.monthly_usd,
            warn_pct=payload.warn_pct,
            updated_at=utc_timestamp(container.clock.now()),
        )
    except BudgetConfigError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message) from exc
    return success_envelope(
        {"thresholds": thresholds.to_contract()},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
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
