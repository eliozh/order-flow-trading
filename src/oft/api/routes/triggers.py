"""Trigger queue: manual evaluate, accept, reject, read (paper only)."""

from __future__ import annotations

import json
from datetime import UTC
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Path, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.trigger import TriggerEvaluateRequest, TriggerRejectRequest
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.trigger_service import (
    TriggerError,
    TriggerNotFound,
    TriggerService,
    trigger_summary,
)
from oft.domain.time import utc_timestamp

router = APIRouter()


def _idempotency_key(presented: str | None) -> str:
    key = (presented or "").strip()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED")
    if len(key) > 200:
        raise ApiError("VALIDATION_FAILED", "Idempotency-Key is too long.")
    return key


def _trigger_service(request: Request) -> TriggerService:
    container = request.app.state.container
    return TriggerService(store=container.store, clock=container.clock)


def _entry_service(request: Request) -> EntryService:
    container = request.app.state.container
    if container.llm_provider is None:
        raise ApiError(
            "LLM_PROVIDER_NOT_CONFIGURED",
            "No LLM provider is configured; the entry re-check cannot run.",
        )
    kill_switch = KillSwitch(
        settings=container.settings, store=container.store, clock=container.clock
    )
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill_switch,
    )


@router.get("/api/v1/triggers")
def list_triggers(
    request: Request,
    opportunity_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    items = [
        trigger_summary(payload)
        for payload in _trigger_service(request).list(opportunity_id=opportunity_id, limit=limit)
    ]
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
    )


@router.get("/api/v1/triggers/{trigger_id}")
def read_trigger(
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    try:
        payload = _trigger_service(request).show(trigger_id)
    except TriggerNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=payload["triggered_at"],
        provenance="MIXED",
        data_quality=payload["snapshot"]["data_quality"],
    )


@router.post("/api/v1/triggers/evaluate")
def evaluate_trigger(
    payload: TriggerEvaluateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    _idempotency_key(idempotency_key)
    as_of = None
    if payload.as_of is not None:
        if payload.as_of.tzinfo is None:
            raise ApiError("VALIDATION_FAILED", "as_of must be timezone-aware UTC.")
        as_of = utc_timestamp(payload.as_of.astimezone(UTC))
    try:
        run = _trigger_service(request).evaluate(
            payload.opportunity_id,
            price=Decimal(payload.price) if payload.price is not None else None,
            as_of=as_of,
            snapshot_id=payload.snapshot_id,
        )
    except TriggerError as exc:
        raise ApiError(exc.code, exc.message) from exc
    data = {
        "opportunity_id": payload.opportunity_id,
        "triggered": run.trigger is not None,
        "trigger": (None if run.trigger is None else json.loads(run.trigger.payload_json)),
        "evaluation": {
            "met": run.evaluation.met,
            "stale": run.evaluation.stale,
            "snapshot_id": run.evaluation.snapshot_id,
            "snapshot_hash": run.evaluation.snapshot_hash,
            "as_of": run.evaluation.as_of,
            "data_quality": run.evaluation.data_quality,
            "reasons": list(run.evaluation.reasons),
            "condition_results": [result.to_contract() for result in run.evaluation.results],
        },
    }
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=run.evaluation.data_quality,
    )


@router.post("/api/v1/triggers/{trigger_id}/accept")
def accept_trigger(
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    key = _idempotency_key(idempotency_key)
    try:
        result = _entry_service(request).accept(trigger_id, idempotency_key=key)
    except EntryRejected as exc:
        raise ApiError(exc.code, exc.message) from exc
    except TriggerNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    return success_envelope(
        {
            "status": result.status,
            "decision_id": result.decision_id,
            "risk_verdict": result.risk_verdict,
            "order": result.order,
            "fill": result.fill,
            "position": result.position,
            "account": result.account,
        },
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality="COMPLETE",
        idempotent_replay=result.idempotent_replay,
    )


@router.post("/api/v1/triggers/{trigger_id}/reject")
def reject_trigger(
    payload: TriggerRejectRequest,
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    _idempotency_key(idempotency_key)
    try:
        result = _entry_service(request).reject(trigger_id, reason=payload.reason)
    except EntryRejected as exc:
        raise ApiError(exc.code, exc.message) from exc
    except TriggerNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    return success_envelope(
        result,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
