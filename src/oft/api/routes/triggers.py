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
from oft.application.opportunity_service import OpportunityNotFound
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


def _scan_provider(container: Any, trigger_id: str) -> str | None:
    """The provider recorded by the scan that produced this trigger, if any."""
    record = container.store.get_trigger_event(trigger_id)
    if record is None:
        return None
    payload = json.loads(record.payload_json)
    opportunity = container.store.get_opportunity(str(payload.get("opportunity_id", "")))
    if opportunity is None:
        return None
    opportunity_payload = json.loads(opportunity.payload_json)
    report_id = str(opportunity_payload.get("source", {}).get("report_id", ""))
    report = container.store.get_report(report_id) if report_id else None
    if report is None or report.llm_json is None:
        return None
    provider = json.loads(report.llm_json).get("provider")
    return provider if isinstance(provider, str) else None


def _entry_service(request: Request, trigger_id: str) -> EntryService:
    container = request.app.state.container
    kill_switch = KillSwitch(
        settings=container.settings, store=container.store, clock=container.clock
    )
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.provider_for(_scan_provider(container, trigger_id)),
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
    except OpportunityNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
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
        result = _entry_service(request, trigger_id).accept(trigger_id, idempotency_key=key)
    except EntryRejected as exc:
        raise ApiError(exc.code, exc.message) from exc
    except TriggerNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except OpportunityNotFound as exc:
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
        result = _entry_service(request, trigger_id).reject(trigger_id, reason=payload.reason)
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
