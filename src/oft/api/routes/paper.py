"""Paper account read models and the kill-switch surface (no live path)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.paper import KillSwitchRequest
from oft.application.kill_switch import InvalidResumeToken, KillSwitch
from oft.application.paper_broker import PaperBroker

router = APIRouter()


def _kill_switch(request: Request) -> KillSwitch:
    container = request.app.state.container
    return KillSwitch(settings=container.settings, store=container.store, clock=container.clock)


def _broker(request: Request) -> PaperBroker:
    container = request.app.state.container
    return PaperBroker(settings=container.settings, store=container.store, clock=container.clock)


@router.get("/api/v1/paper/balance")
def paper_balance(request: Request) -> dict[str, Any]:
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
def paper_orders(request: Request, limit: int = 100) -> dict[str, Any]:
    container = request.app.state.container
    items = [json.loads(record.payload_json) for record in container.store.list_orders(limit=limit)]
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
    )


@router.get("/api/v1/paper/fills")
def paper_fills(request: Request, limit: int = 100) -> dict[str, Any]:
    container = request.app.state.container
    items = [json.loads(record.payload_json) for record in container.store.list_fills(limit=limit)]
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
    )


@router.get("/api/v1/paper/equity")
def paper_equity(request: Request) -> dict[str, Any]:
    account = _broker(request).account()
    positions = request.app.state.container.store.list_positions(state="OPEN")
    return success_envelope(
        {
            "account_id": account.account_id,
            "balance": account.balance,
            "equity": account.balance,
            "open_positions": len(positions),
            "note": "PnL and funding are out of scope in this slice; equity equals balance.",
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/kill")
def kill_status(request: Request) -> dict[str, Any]:
    status = _kill_switch(request).status()
    return success_envelope(
        status,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.post("/api/v1/kill")
def set_kill_switch(payload: KillSwitchRequest, request: Request) -> dict[str, Any]:
    kill = _kill_switch(request)
    if payload.active:
        status = kill.activate()
    else:
        if payload.token is None:
            raise ApiError(
                "VALIDATION_FAILED",
                "deactivation requires the typed RESUME token",
            )
        try:
            status = kill.deactivate(token=payload.token)
        except InvalidResumeToken as exc:
            raise ApiError("VALIDATION_FAILED", exc.message) from exc
    return success_envelope(
        status,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
