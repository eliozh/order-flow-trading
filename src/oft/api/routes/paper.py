"""Paper account read models and the kill-switch surface (no live path)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.kill_switch import InvalidResumeToken, KillSwitch
from oft.application.paper_broker import PaperBroker

router = APIRouter()


def _kill_switch(request: Request) -> KillSwitch:
    container = request.app.state.container
    return KillSwitch(settings=container.settings, store=container.store, clock=container.clock)


def _broker(request: Request) -> PaperBroker:
    container = request.app.state.container
    return PaperBroker(settings=container.settings, store=container.store, clock=container.clock)


@router.get("/api/v1/paper/account")
def paper_account(request: Request) -> dict[str, Any]:
    account = _broker(request).account()
    return success_envelope(
        {
            "account_id": account.account_id,
            "environment": account.environment,
            "currency": account.currency,
            "initial_balance": account.initial_balance,
            "balance": account.balance,
            "updated_at": account.updated_at,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/paper/orders")
def paper_orders(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    store = request.app.state.container.store
    items = [
        json.loads(record.payload_json) for record in store.list_orders(limit=limit, offset=offset)
    ]
    total = store.count_orders()
    has_next = offset + len(items) < total
    return success_envelope(
        {"items": items, "total": total},
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


@router.get("/api/v1/paper/fills")
def paper_fills(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    store = request.app.state.container.store
    items = [
        json.loads(record.payload_json) for record in store.list_fills(limit=limit, offset=offset)
    ]
    total = store.count_fills()
    has_next = offset + len(items) < total
    return success_envelope(
        {"items": items, "total": total},
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


@router.get("/api/v1/paper/equity")
def paper_equity(request: Request) -> dict[str, Any]:
    account = _broker(request).account()
    open_positions = request.app.state.container.store.count_positions(state="OPEN")
    return success_envelope(
        {
            "account_id": account.account_id,
            "balance": account.balance,
            "equity": account.balance,
            "open_positions": open_positions,
            "note": "PnL and funding are out of scope in this slice; equity equals balance.",
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/kill-switch")
def kill_status(request: Request) -> dict[str, Any]:
    status = _kill_switch(request).status()
    return success_envelope(
        status,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.post("/api/v1/kill-switch")
def activate_kill_switch(request: Request) -> dict[str, Any]:
    """Activate immediately; ADR-0003 makes activation always allowed."""
    status = _kill_switch(request).activate()
    return success_envelope(
        status,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.delete("/api/v1/kill-switch")
def deactivate_kill_switch(
    request: Request, token: str | None = Query(default=None, max_length=32)
) -> dict[str, Any]:
    if token is None:
        raise ApiError(
            "VALIDATION_FAILED",
            "deactivation requires the typed RESUME token",
        )
    try:
        status = _kill_switch(request).deactivate(token=token)
    except InvalidResumeToken as exc:
        raise ApiError("VALIDATION_FAILED", exc.message) from exc
    return success_envelope(
        status,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
