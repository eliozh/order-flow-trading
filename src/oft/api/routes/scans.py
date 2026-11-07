"""Scan routes: manual metrics-only scans plus the legacy fixture debug path."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.report import ScanCreateRequest
from oft.application.fixture_scan import get_scan, run_fixture_scan
from oft.application.scan_service import (
    ScanError,
    ScanRequest,
    ScanService,
    public_scan_payload,
    report_summary,
)
from oft.config import Environment
from oft.domain.time import utc_timestamp
from oft.ports.store import IdempotencyRecord

router = APIRouter()

_IDEMPOTENCY_TTL = timedelta(hours=24)


def _validated_fixture_path(raw: str, fixtures_dir: Path) -> str:
    """Resolve the fixture path and reject anything outside the fixtures root."""
    root = fixtures_dir.expanduser().resolve()
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_relative_to(root):
        raise ApiError(
            "VALIDATION_FAILED",
            "fixture_path must be inside the configured fixtures directory.",
            details=[
                {
                    "field": "fixture_path",
                    "code": "path_forbidden",
                    "message": f"{raw} is outside {root}",
                }
            ],
        )
    return str(candidate)


def _scope(request: Request) -> str:
    settings = request.app.state.container.settings
    return f"{settings.env.value}:POST:{request.url.path}"


def _request_hash(payload: ScanCreateRequest) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay(record: IdempotencyRecord) -> JSONResponse:
    envelope = json.loads(record.response_json)
    envelope["meta"]["idempotent_replay"] = True
    return JSONResponse(status_code=record.response_status, content=envelope)


def _idempotency_key(presented: str | None) -> str:
    key = (presented or "").strip()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED")
    if len(key) > 200:
        raise ApiError("VALIDATION_FAILED", "Idempotency-Key is too long.")
    return key


def _idempotency_record(
    request: Request,
    *,
    key: str,
    request_hash: str,
    status: int,
    response_json: str,
) -> IdempotencyRecord:
    container = request.app.state.container
    now = container.clock.now()
    return IdempotencyRecord(
        scope=_scope(request),
        key=key,
        request_hash=request_hash,
        response_status=status,
        response_json=response_json,
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + _IDEMPOTENCY_TTL),
    )


def _complete_idempotency(
    request: Request, *, key: str, request_hash: str, envelope: dict[str, Any]
) -> None:
    container = request.app.state.container
    container.store.complete_idempotency(
        _idempotency_record(
            request,
            key=key,
            request_hash=request_hash,
            status=201,
            response_json=json.dumps(envelope),
        )
    )


def _run_fixture(request: Request, payload: ScanCreateRequest) -> dict[str, Any]:
    container = request.app.state.container
    assert payload.fixture_path is not None
    fixture_path = _validated_fixture_path(payload.fixture_path, container.settings.fixtures_dir)
    lock = request.app.state.scan_lock
    if not lock.acquire(blocking=False):
        raise ApiError("SCAN_ALREADY_RUNNING")
    try:
        record = run_fixture_scan(
            container,
            fixture_path=fixture_path,
            symbol=payload.symbol,
            period=payload.period,
            metrics_only=payload.metrics_only,
        )
    finally:
        lock.release()
    return success_envelope(
        public_scan_payload(record),
        request_id=new_request_id(),
        generated_at=record.finished_at,
        provenance="CALC",
        data_quality=record.data_quality,
    )


def _run_metrics_scan(request: Request, payload: ScanCreateRequest) -> dict[str, Any]:
    container = request.app.state.container
    try:
        run = ScanService(container=container, lock=container.scan_lock).run(
            ScanRequest(
                symbol=payload.symbol,
                period=payload.period,
                source=payload.source,
                metrics_only=payload.metrics_only,
            )
        )
    except ScanError as exc:
        raise ApiError(exc.code, exc.message) from exc
    return success_envelope(
        {
            "scan": public_scan_payload(run.scan),
            "report": report_summary(run.report),
            "report_id": run.report.report_id,
        },
        request_id=new_request_id(),
        generated_at=run.report.generated_at,
        provenance="CALC",
        data_quality=run.report.data_quality,
    )


@router.post("/api/v1/scans", status_code=201)
def create_scan(
    payload: ScanCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Run one manual metrics-only scan (fixture_path keeps the debug path).

    ``Idempotency-Key`` is mandatory for the metrics-only mutation surface. A
    replay of the same key + payload returns the stored response with
    ``meta.idempotent_replay=true``; a different payload is a 409 conflict.
    """
    container = request.app.state.container
    if payload.fixture_path is not None:
        if (
            container.settings.env != Environment.TEST
            or not container.settings.allow_fixture_scan_debug
        ):
            raise ApiError(
                "VALIDATION_FAILED",
                "fixture_path debug scans require the test environment and "
                "OFT_ALLOW_FIXTURE_SCAN_DEBUG=true.",
            )
        return _run_fixture(request, payload)

    key = _idempotency_key(idempotency_key)
    scoped_key = _scope(request)
    request_hash = _request_hash(payload)
    now = utc_timestamp(container.clock.now())
    container.store.purge_expired_idempotency(now=now)
    reservation = _idempotency_record(
        request,
        key=key,
        request_hash=request_hash,
        status=0,
        response_json="",
    )
    if not container.store.reserve_idempotency(reservation):
        existing = container.store.get_idempotency(scope=scoped_key, key=key)
        if existing is not None and existing.expires_at <= now:
            container.store.delete_idempotency(scope=scoped_key, key=key)
            if container.store.reserve_idempotency(reservation):
                existing = None
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ApiError("IDEMPOTENCY_CONFLICT")
            if existing.response_json:
                return _replay(existing)
            raise ApiError(
                "IDEMPOTENCY_CONFLICT",
                "A request with this Idempotency-Key is still running.",
            )

    try:
        envelope = _run_metrics_scan(request, payload)
    except BaseException:
        container.store.delete_idempotency(scope=scoped_key, key=key)
        raise
    _complete_idempotency(request, key=key, request_hash=request_hash, envelope=envelope)
    return JSONResponse(status_code=201, content=envelope)


@router.get("/api/v1/scans")
def list_scans(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List recent scans (newest first) for ``oft scan history``."""
    container = request.app.state.container
    container.store.fail_stale_scans(now=utc_timestamp(container.clock.now()))
    records = container.store.list_scans(limit=limit, offset=offset)
    total = container.store.count_scans()
    items = [public_scan_payload(record) for record in records]
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
            "next_cursor": str(offset + len(items)) if has_next else None,
            "total": total,
        },
    )


@router.get("/api/v1/scans/{scan_id}")
def read_scan(
    request: Request,
    scan_id: str = PathParam(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    container = request.app.state.container
    container.store.fail_stale_scans(now=utc_timestamp(container.clock.now()))
    record = get_scan(container, scan_id)
    if record is None:
        raise ApiError("NOT_FOUND", f"Scan {scan_id} does not exist.")
    return success_envelope(
        public_scan_payload(record),
        request_id=new_request_id(),
        generated_at=record.finished_at,
        provenance="CALC",
        data_quality=record.data_quality,
    )
