"""Read-only opportunity ledger routes (no state-override surface exists)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Request, Response

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.opportunity_service import (
    OpportunityContractError,
    OpportunityNotFound,
    OpportunityService,
    opportunity_payload,
    opportunity_summary,
)

router = APIRouter()

_OPPORTUNITY_STATES = ("OPEN", "TRIGGERED", "EXPIRED", "INVALIDATED")


@router.get("/api/v1/opportunities")
def list_opportunities(
    request: Request,
    state: str | None = Query(
        default=None,
        pattern=r"^(OPEN|open|TRIGGERED|triggered|EXPIRED|expired|INVALIDATED|invalidated)$",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    container = request.app.state.container
    service = OpportunityService(store=container.store, clock=container.clock)
    try:
        items = service.list(state=state, limit=limit, offset=offset)
        total = service.count(state=state)
        # Per-state counts come from SQL, so tab counts never depend on the
        # fetched page and cannot under-report above the page size.
        counts = {name: service.count(state=name) for name in _OPPORTUNITY_STATES}
    except OpportunityContractError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc
    payloads = [opportunity_summary(item) for item in items]
    has_next = offset + len(payloads) < total
    return success_envelope(
        {"items": payloads, "total": total, "counts": counts},
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality="COMPLETE" if payloads else "MISSING",
        pagination={
            "limit": limit,
            "cursor": str(offset),
            "has_next": has_next,
            "next_cursor": str(offset + limit) if has_next else None,
            "total": total,
        },
    )


@router.get("/api/v1/opportunities/{opportunity_id}")
def read_opportunity(
    request: Request,
    opportunity_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    container = request.app.state.container
    service = OpportunityService(store=container.store, clock=container.clock)
    try:
        opportunity = service.show(opportunity_id)
    except OpportunityNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except OpportunityContractError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc
    return success_envelope(
        opportunity_payload(opportunity),
        request_id=new_request_id(),
        generated_at=opportunity.updated_at,
        provenance="MIXED",
        data_quality=str(opportunity.data_quality.get("status", "COMPLETE")),
    )


@router.get("/api/v1/opportunities/{opportunity_id}/timeline")
def read_opportunity_timeline(
    request: Request,
    opportunity_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    container = request.app.state.container
    service = OpportunityService(store=container.store, clock=container.clock)
    try:
        events = service.timeline(opportunity_id)
    except OpportunityNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except OpportunityContractError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc
    return success_envelope(
        {"opportunity_id": opportunity_id, "items": list(events)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if events else "MISSING",
    )


@router.get("/api/v1/opportunities/{opportunity_id}/conditions")
def read_opportunity_conditions(
    request: Request,
    opportunity_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """Display-only evaluation of every condition; nothing is persisted."""
    container = request.app.state.container
    service = OpportunityService(store=container.store, clock=container.clock)
    try:
        payload = service.evaluate_display(opportunity_id)
    except OpportunityNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except OpportunityContractError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=str(payload["as_of"]),
        provenance="CALC",
        data_quality=str(payload["data_quality"]),
    )


@router.get("/api/v1/opportunities/{opportunity_id}/export")
def export_opportunity(
    request: Request,
    response: Response,
    opportunity_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """The contract envelope as a downloadable attachment (read-only)."""
    container = request.app.state.container
    service = OpportunityService(store=container.store, clock=container.clock)
    try:
        opportunity = service.show(opportunity_id)
    except OpportunityNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except OpportunityContractError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc
    response.headers["Content-Disposition"] = (
        f'attachment; filename="opportunity-{opportunity_id}.json"'
    )
    return success_envelope(
        opportunity_payload(opportunity),
        request_id=new_request_id(),
        generated_at=opportunity.updated_at,
        provenance="MIXED",
        data_quality=str(opportunity.data_quality.get("status", "COMPLETE")),
    )
