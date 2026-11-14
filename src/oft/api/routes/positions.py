"""Read the position ledger, its accounting history, and run one management action."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Path, Query, Request, Response

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ERROR_CATALOG, ApiError
from oft.api.idempotency import (
    complete,
    release,
    reserve,
)
from oft.api.idempotency import (
    idempotency_key as require_idempotency_key,
)
from oft.api.idempotency import (
    request_hash as compute_request_hash,
)
from oft.api.schemas.position import (
    Envelope,
    PositionListData,
    PositionManageData,
    PositionManageRequest,
    PositionMonitorData,
)
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    ManagementRejected,
    ManagementResult,
    ManagementService,
)
from oft.application.position_service import (
    DATA_STALE,
    PositionCloseError,
    PositionNotFound,
    PositionService,
)
from oft.domain.risk import ManagementStatus

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


def _management_service(request: Request, provider: str | None) -> ManagementService:
    container = request.app.state.container
    kill_switch = KillSwitch(
        settings=container.settings, store=container.store, clock=container.clock
    )
    return ManagementService(
        store=container.store,
        clock=container.clock,
        provider=container.provider_for(provider),
        settings=container.settings,
        kill_switch=kill_switch,
        runtime=container.llm_runtime,
    )


def _api_error(exc: PositionCloseError) -> ApiError:
    code = _ERROR_MAP.get(exc.code, exc.code)
    if code not in ERROR_CATALOG:
        code = "CONFLICT"
    return ApiError(code, exc.message)


def _management_error(exc: ManagementRejected) -> ApiError:
    """Map a management code through the same catalog fallback as ``_api_error``.

    ``ManagementRejected`` can carry a service-only code (for example
    ``POSITION_NOT_FOUND`` on a replay whose row vanished); passing it straight
    to ``ApiError`` would raise ``KeyError`` and turn a typed 404 into a 500.
    """
    code = _ERROR_MAP.get(exc.code, exc.code)
    if code not in ERROR_CATALOG:
        code = "CONFLICT"
    details = (
        [{"field": exc.field, "code": exc.code, "message": exc.message}]
        if exc.field is not None
        else []
    )
    return ApiError(code, exc.message, details=details)


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


@router.post(
    "/api/v1/positions/{position_id}/manage",
    response_model=Envelope[PositionManageData],
)
def manage_position(
    payload: PositionManageRequest,
    request: Request,
    response: Response,
    position_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Obtain a validated management decision and execute it (paper only)."""
    key = require_idempotency_key(idempotency_key)
    payload_hash = compute_request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
    if replay is not None:
        return replay
    try:
        result = _management_service(request, payload.provider).manage(
            position_id, idempotency_key=key, provider_name=payload.provider
        )
    except ManagementRejected as exc:
        release(request, key=key)
        raise _management_error(exc) from exc
    except PositionNotFound as exc:
        release(request, key=key)
        raise ApiError("NOT_FOUND", exc.message) from exc
    except PositionCloseError as exc:
        release(request, key=key)
        raise _api_error(exc) from exc
    except BaseException:
        release(request, key=key)
        raise
    contract = result.to_contract()
    managed = contract.get("position")
    if isinstance(managed, dict):
        # A management result carries a position snapshot; expose the same
        # server-resolved mark the list/detail read models do. Decorate before
        # ``complete`` so a replay returns the identical envelope.
        contract["position"] = _service(request).with_resolved_mark(managed)
    envelope = success_envelope(
        contract,
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality=_management_quality(result),
        idempotent_replay=result.idempotent_replay,
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=200)
    response.status_code = 200
    return envelope


def _management_quality(result: ManagementResult) -> str:
    """PARTIAL when risk evidence is missing or names an unmeasured input.

    An executed action with no persisted risk verdict is never reported
    COMPLETE; HOLD/NO_ACTION legitimately carry no risk evidence.
    """
    if result.executed and result.risk is None:
        return "PARTIAL"
    if result.status is ManagementStatus.BLOCKED:
        return "PARTIAL"
    risk = result.risk
    if isinstance(risk, dict) and risk.get("stale_inputs"):
        return "PARTIAL"
    if isinstance(risk, dict) and risk.get("verdict") == "BLOCK":
        return "PARTIAL"
    return "COMPLETE"
