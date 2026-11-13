"""Read the position ledger, its accounting history, and run one management tick."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ERROR_CATALOG, ApiError
from oft.api.schemas.position import Envelope, PositionListData, PositionMonitorData
from oft.application.position_service import (
    DATA_STALE,
    PositionCloseError,
    PositionNotFound,
    PositionService,
)

router = APIRouter()

_POSITION_STATES = ("OPEN", "CLOSED", "LIQUIDATED", "RECONCILING")
_STATE_PATTERN = r"^(OPEN|open|CLOSED|closed|LIQUIDATED|liquidated|RECONCILING|reconciling)$"
_ERROR_MAP = {
    "POSITION_STATE_INVALID": "CONFLICT",
    "POSITION_NOT_FOUND": "NOT_FOUND",
}


def _service(request: Request) -> PositionService:
    container = request.app.state.container
    return PositionService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
    )


def _api_error(exc: PositionCloseError) -> ApiError:
    code = _ERROR_MAP.get(exc.code, exc.code)
    if code not in ERROR_CATALOG:
        code = "CONFLICT"
    return ApiError(code, exc.message)


@router.get("/api/v1/positions", response_model=Envelope[PositionListData])
def list_positions(
    request: Request,
    state: str | None = Query(default=None, pattern=_STATE_PATTERN),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    service = _service(request)
    resolved_state = state.upper() if state else None
    try:
        items = list(service.list(state=resolved_state, limit=limit, offset=offset))
        total = service.count(state=resolved_state)
        counts = {name: service.count(state=name) for name in _POSITION_STATES}
    except PositionCloseError as exc:
        raise _api_error(exc) from exc
    has_next = offset + len(items) < total
    return success_envelope(
        {"items": items, "total": total, "counts": counts},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
        pagination={
            "limit": limit,
            "cursor": str(offset),
            "has_next": has_next,
            "next_cursor": str(offset + limit) if has_next else None,
            "total": total,
        },
    )


@router.get("/api/v1/positions/{position_id}", response_model=Envelope[dict[str, Any]])
def read_position(
    request: Request,
    position_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    try:
        payload = _service(request).show(position_id)
    except PositionNotFound as exc:
        raise ApiError("NOT_FOUND", exc.message) from exc
    except PositionCloseError as exc:
        raise _api_error(exc) from exc
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=str(payload.get("updated_at") or ""),
        provenance="CALC",
        data_quality=str((payload.get("data_quality") or {}).get("status", "COMPLETE")),
    )


@router.get(
    "/api/v1/positions/{position_id}/history",
    response_model=Envelope[dict[str, Any]],
)
def read_position_history(
    request: Request,
    position_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """The position's management timeline, funding accruals, and PnL terms."""
    try:
        payload = _service(request).history(position_id)
    except PositionNotFound as exc:
        raise ApiError("NOT_FOUND", exc.message) from exc
    except PositionCloseError as exc:
        raise _api_error(exc) from exc
    return success_envelope(
        payload,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=str(
            ((payload.get("pnl") or {}).get("data_quality") or {}).get("status", "COMPLETE")
        ),
    )


@router.post(
    "/api/v1/positions/{position_id}/monitor",
    response_model=Envelope[PositionMonitorData],
)
def monitor_position(
    request: Request,
    position_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """Accrue recorded funding and run one deterministic stop-touch tick (idempotent)."""
    try:
        result = _service(request).monitor(position_id)
    except PositionNotFound as exc:
        raise ApiError("NOT_FOUND", exc.message) from exc
    except PositionCloseError as exc:
        raise _api_error(exc) from exc
    quality = "PARTIAL" if result.reason_code == DATA_STALE else "COMPLETE"
    return success_envelope(
        result.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=quality,
        idempotent_replay=result.idempotent_replay,
    )
