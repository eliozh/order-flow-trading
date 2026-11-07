"""Trigger queue: manual evaluate, accept, reject, read (paper only)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.trigger import (
    Envelope,
    TriggerAcceptData,
    TriggerEvaluateData,
    TriggerEvaluateRequest,
    TriggerRejectRequest,
)
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
from oft.ports.store import IdempotencyRecord

router = APIRouter()

_IDEMPOTENCY_LEASE = timedelta(seconds=30)
_IDEMPOTENCY_TTL = timedelta(hours=24)


def _idempotency_key(presented: str | None) -> str:
    key = (presented or "").strip()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED")
    if len(key) > 200:
        raise ApiError("VALIDATION_FAILED", "Idempotency-Key is too long.")
    return key


def _scope(request: Request) -> str:
    settings = request.app.state.container.settings
    return f"{settings.env.value}:{request.method}:{request.url.path}"


def _request_hash(payload: Any) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reserve(request: Request, *, key: str, request_hash: str) -> JSONResponse | None:
    """Reserve ``(method, path, key)``; return a stored replay if one exists."""
    container = request.app.state.container
    now = container.clock.now()
    scope = _scope(request)
    container.store.purge_expired_idempotency(now=utc_timestamp(now))
    reservation = IdempotencyRecord(
        scope=scope,
        key=key,
        request_hash=request_hash,
        response_status=0,
        response_json="",
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + _IDEMPOTENCY_LEASE),
    )
    if container.store.reserve_idempotency(reservation):
        return None
    existing = container.store.get_idempotency(scope=scope, key=key)
    if existing is not None and existing.request_hash != request_hash:
        raise ApiError("IDEMPOTENCY_CONFLICT")
    if existing is not None and existing.response_json:
        envelope = json.loads(existing.response_json)
        envelope["meta"]["idempotent_replay"] = True
        return JSONResponse(status_code=existing.response_status, content=envelope)
    raise ApiError("IDEMPOTENCY_CONFLICT", "A request with this Idempotency-Key is still running.")


def _complete(
    request: Request, *, key: str, request_hash: str, envelope: dict[str, Any], status: int
) -> None:
    container = request.app.state.container
    now = container.clock.now()
    container.store.complete_idempotency(
        IdempotencyRecord(
            scope=_scope(request),
            key=key,
            request_hash=request_hash,
            response_status=status,
            response_json=json.dumps(envelope),
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + _IDEMPOTENCY_TTL),
        )
    )


def _release(request: Request, *, key: str) -> None:
    request.app.state.container.store.delete_idempotency(scope=_scope(request), key=key)


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
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = _trigger_service(request)
    items = [
        trigger_summary(payload)
        for payload in service.list(opportunity_id=opportunity_id, limit=limit, offset=offset)
    ]
    total = service.count(opportunity_id=opportunity_id)
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


@router.get("/api/v1/triggers/{trigger_id}")
def read_trigger(
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    try:
        payload = _trigger_service(request).show(trigger_id)
    except TriggerNotFound as exc:
        raise ApiError("NOT_FOUND", str(exc)) from exc
    record = request.app.state.container.store.get_trigger_event(trigger_id)
    snapshot = payload.get("snapshot") or {}
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=payload.get("triggered_at") or (record.triggered_at if record else None),
        provenance="MIXED",
        data_quality=str(snapshot.get("data_quality", "MISSING")),
    )


@router.post("/api/v1/triggers/evaluate", response_model=Envelope[TriggerEvaluateData])
def evaluate_trigger(
    payload: TriggerEvaluateRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    as_of = None
    if payload.as_of is not None:
        if payload.as_of.tzinfo is None:
            _release(request, key=key)
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
        _release(request, key=key)
        raise ApiError(exc.code, exc.message) from exc
    except OpportunityNotFound as exc:
        _release(request, key=key)
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except BaseException:
        _release(request, key=key)
        raise
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
    status = 201 if run.created else 200
    envelope = success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=run.evaluation.data_quality,
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=status)
    response.status_code = status
    return envelope


@router.post("/api/v1/triggers/{trigger_id}/recheck", response_model=Envelope[dict[str, Any]])
def recheck_trigger(
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Run the LLM entry re-check for one trigger (ADR-0003 surface)."""
    key = _idempotency_key(idempotency_key)
    request_hash = hashlib.sha256(
        json.dumps({"trigger_id": trigger_id}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    try:
        result = _entry_service(request, trigger_id).recheck(trigger_id)
    except EntryRejected as exc:
        _release(request, key=key)
        raise ApiError(exc.code, exc.message) from exc
    except TriggerNotFound as exc:
        _release(request, key=key)
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except BaseException:
        _release(request, key=key)
        raise
    envelope = success_envelope(
        result,
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality="COMPLETE",
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=200)
    return envelope


@router.post("/api/v1/triggers/{trigger_id}/accept", response_model=Envelope[TriggerAcceptData])
def accept_trigger(
    request: Request,
    response: Response,
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
    response.status_code = 200 if result.idempotent_replay else 201
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


@router.post("/api/v1/triggers/{trigger_id}/reject", response_model=Envelope[dict[str, Any]])
def reject_trigger(
    payload: TriggerRejectRequest,
    request: Request,
    trigger_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    try:
        result = _entry_service(request, trigger_id).reject(trigger_id, reason=payload.reason)
    except EntryRejected as exc:
        _release(request, key=key)
        raise ApiError(exc.code, exc.message) from exc
    except TriggerNotFound as exc:
        _release(request, key=key)
        raise ApiError("NOT_FOUND", str(exc)) from exc
    except BaseException:
        _release(request, key=key)
        raise
    envelope = success_envelope(
        result,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=200)
    return envelope
