"""Scan routes: manual metrics-only scans plus the legacy fixture debug path."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Header, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.report import ScanCreateRequest
from oft.application.fixture_scan import get_scan, run_fixture_scan
from oft.application.jobs import JobLedger, ScheduledScanRunner
from oft.application.scan_service import (
    TERMINAL_STAGES,
    ScanAlreadyRunning,
    ScanError,
    ScanRequest,
    ScanService,
    _scan_id,
    public_scan_payload,
    report_summary,
)
from oft.config import Environment
from oft.domain.time import utc_timestamp
from oft.ports.store import IdempotencyRecord, ScanRecord, ScanSymbolRecord

logger = structlog.get_logger(__name__)

router = APIRouter()

_IDEMPOTENCY_TTL = timedelta(hours=24)
_IDEMPOTENCY_LEASE = timedelta(seconds=30)
_SCAN_HEARTBEAT_SECONDS = 10.0
_IDEMPOTENCY_HEARTBEAT = timedelta(seconds=10)
_FAILED_STAGE_BY_CODE = {
    "DATA_UNAVAILABLE": "DATA",
    "METRICS_INCOMPLETE": "METRICS",
}


def _failure_stage(exc: BaseException, code: str) -> str:
    """The stage that failed, from the scan service or the error code."""
    stage = getattr(exc, "failed_stage", None)
    if stage:
        return str(stage)
    return _FAILED_STAGE_BY_CODE.get(code) or "REPORT"


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
    ttl: timedelta | None = None,
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
        expires_at=utc_timestamp(now + (ttl if ttl is not None else _IDEMPOTENCY_TTL)),
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
    assert payload.symbol is not None
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


def _scan_error_details(exc: ScanError) -> list[dict[str, Any]] | None:
    if exc.code != "SCAN_ALREADY_RUNNING":
        return None
    running_scan_id = getattr(exc, "running_scan_id", None)
    return [
        {
            "field": "scan_id",
            "code": "scan_already_running",
            "message": str(running_scan_id or "unknown"),
        }
    ]


def _delivery() -> dict[str, object]:
    return {
        "configured": False,
        "reason": "TELEGRAM_NOT_CONFIGURED: Telegram delivery is not configured in this build.",
    }


def _report_links(rows: tuple[ScanSymbolRecord, ...]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for row in rows:
        if row.report_id:
            links.append(
                {
                    "symbol": row.symbol,
                    "report_id": row.report_id,
                    "href": f"/reports/{row.report_id}",
                }
            )
    return links


def _failed_stage(row: ScanSymbolRecord) -> str:
    """Return the stage a FAILED row actually failed in.

    New rows persist ``failed_stage``; legacy rows fall back to the error code
    so the four cells stay honest for both shapes.
    """
    if row.failed_stage:
        return row.failed_stage
    if row.error_code == "DATA_UNAVAILABLE":
        return "DATA"
    if row.error_code == "METRICS_INCOMPLETE":
        return "METRICS"
    return "REPORT"


def _symbol_stage_flags(row: ScanSymbolRecord) -> dict[str, str]:
    """Derive the per-symbol DATA/METRICS/LLM/REPORT cells for S03.2."""
    data = metrics = llm = report = "PENDING"
    if row.stage == "DATA":
        data = "RUNNING"
    elif row.stage == "METRICS":
        data, metrics = "DONE", "RUNNING"
    elif row.stage == "LLM":
        data = metrics = "DONE"
        llm = "RUNNING"
    elif row.stage == "REPORT":
        data = metrics = llm = "DONE"
        report = "RUNNING"
    elif row.stage in {"DONE", "DEGRADED"}:
        data = metrics = "DONE"
        llm = (
            "DONE"
            if row.llm_status in {"VALID", "REPAIRED"}
            else "FAILED"
            if row.llm_status == "REJECTED"
            else "SKIPPED"
        )
        report = "DONE" if row.report_id else "SKIPPED"
    elif row.stage == "FAILED":
        stage = _failed_stage(row)
        if stage == "DATA":
            data = "FAILED"
            metrics = llm = report = "SKIPPED"
        elif stage == "METRICS":
            data, metrics = "DONE", "FAILED"
            llm = report = "SKIPPED"
        elif stage == "LLM":
            data = metrics = "DONE"
            llm, report = "FAILED", "SKIPPED"
        else:
            data = metrics = "DONE"
            llm = "DONE" if row.llm_status is not None else "SKIPPED"
            report = "FAILED"
    return {
        "data_stage": data,
        "metrics_stage": metrics,
        "llm_stage": llm,
        "report_stage": report,
    }


def _symbol_row(row: ScanSymbolRecord) -> dict[str, object]:
    return {
        "symbol": row.symbol,
        "stage": row.stage,
        "report_id": row.report_id,
        "data_quality": row.data_quality,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "failed_stage": row.failed_stage,
        "llm_status": row.llm_status,
        "llm_tokens": row.llm_tokens,
        "cost_usd": row.cost_usd,
        "cost_unpriced": row.cost_unpriced,
        **_symbol_stage_flags(row),
    }


def _cost_totals(rows: tuple[ScanSymbolRecord, ...]) -> tuple[Decimal, bool]:
    """Aggregate child costs; an unparseable amount is surfaced as unpriced.

    A failure row carries no LLM usage, so its default ``cost_unpriced`` must
    not mark the whole batch unpriced. Only configured-LLM rows participate in
    the unpriced flag.
    """
    cost = Decimal("0")
    unpriced = False
    for row in rows:
        try:
            cost += Decimal(row.cost_usd)
        except (InvalidOperation, TypeError, ValueError):
            unpriced = True
            logger.warning(
                "scan_cost_unparseable",
                symbol=row.symbol,
                cost_usd=row.cost_usd,
            )
            continue
        if row.llm_status is not None:
            unpriced = unpriced or row.cost_unpriced
    return cost, unpriced


def _batch_status(rows: tuple[ScanSymbolRecord, ...], *, running: bool) -> str:
    if running and any(row.stage not in TERMINAL_STAGES for row in rows):
        return "RUNNING"
    if not rows:
        return "RUNNING" if running else "FAILED"
    failed = sum(1 for row in rows if row.stage == "FAILED")
    degraded = sum(1 for row in rows if row.stage == "DEGRADED")
    if failed == len(rows):
        return "FAILED"
    if failed or degraded:
        return "PARTIAL"
    if all(row.stage in TERMINAL_STAGES for row in rows):
        return "COMPLETE"
    return "RUNNING" if running else "FAILED"


def _batch_children(container: Any, run_id: str) -> tuple[ScanRecord, ...]:
    """One indexed lookup per batch (never a scan of the newest 500 rows)."""
    children: tuple[ScanRecord, ...] = container.store.list_scans_by_batch(run_id)
    return children


def _batch_period(children: tuple[ScanRecord, ...], run: Any) -> str:
    if children:
        return str(children[0].period)
    # A batch with no surviving child (for example, every symbol failed) still
    # reports the period it was requested with.
    return str(getattr(run, "period", None) or "4h")


def _batch_source_mode(children: tuple[ScanRecord, ...]) -> str:
    for child in children:
        if child.source_mode == "LIVE":
            return "LIVE"
    return children[0].source_mode if children else "BACKFILL"


def _batch_data_quality(children: tuple[ScanRecord, ...]) -> str:
    """Derive data quality from child scans only.

    LLM rejection/degradation is an LLM outcome, not a data gap: a batch whose
    children have COMPLETE data stays COMPLETE even when STATUS is PARTIAL.
    """
    if not children:
        return "MISSING"
    qualities = {child.data_quality or "MISSING" for child in children}
    if "MISSING" in qualities:
        return "MISSING"
    if qualities - {"COMPLETE"}:
        return "PARTIAL"
    return "COMPLETE"


def _aggregate_llm_status(rows: tuple[ScanSymbolRecord, ...]) -> str | None:
    statuses = [row.llm_status for row in rows if row.llm_status]
    if "REJECTED" in statuses:
        return "REJECTED"
    if any(status in {"VALID", "REPAIRED"} for status in statuses):
        return "VALID"
    return None


def _batch_scan_payload(
    container: Any,
    run: Any,
    *,
    period: str | None = None,
) -> dict[str, object]:
    """Render a batch job run with its per-symbol stages and cost totals."""
    rows = container.store.list_scan_symbols(run.run_id)
    children = _batch_children(container, run.run_id)
    resolved_period = period or _batch_period(children, run)
    status = _batch_status(rows, running=run.status == "RUNNING")
    completed = sum(1 for row in rows if row.stage == "DONE")
    degraded = sum(1 for row in rows if row.stage == "DEGRADED")
    failed = sum(1 for row in rows if row.stage == "FAILED")
    tokens = sum(row.llm_tokens for row in rows)
    cost, unpriced = _cost_totals(rows)
    window_starts = [child.window_start for child in children if child.window_start]
    window_ends = [child.window_end for child in children if child.window_end]
    report_ids = [row.report_id for row in rows if row.report_id]
    return {
        "scan_id": run.run_id,
        "batch_id": run.run_id,
        "status": status,
        "trigger_source": run.trigger_source,
        "period": resolved_period,
        "started_at": run.started_at,
        "finished_at": run.finished_at or "",
        "error_code": run.error_code,
        "error_message": run.message,
        "data_quality": _batch_data_quality(children),
        "source_mode": _batch_source_mode(children),
        "window_start": min(window_starts) if window_starts else "",
        "window_end": max(window_ends) if window_ends else "",
        "trade_count": sum(child.trade_count for child in children),
        "symbols_total": len(rows),
        "symbols_completed": completed,
        "symbols_degraded": degraded,
        "symbols_failed": failed,
        "total_tokens": tokens,
        "total_cost_usd": format(cost, "f"),
        "cost_unpriced": unpriced,
        "llm_status": _aggregate_llm_status(rows),
        "report_id": report_ids[0] if report_ids else None,
        "symbols": [_symbol_row(row) for row in rows],
        "report_links": _report_links(rows),
        "report_ids": report_ids,
        "delivery": _delivery(),
    }


def _scan_detail_payload(container: Any, record: ScanRecord) -> dict[str, object]:
    """Single-scan detail enriched with the same per-symbol stage shape."""
    payload = public_scan_payload(record)
    if record.fixture_path:
        # The legacy fixture debug path keeps its original flat shape.
        return payload
    rows = container.store.list_scan_symbols(record.batch_id or record.scan_id)
    total_tokens = record.llm_tokens
    total_cost = record.cost_usd
    cost_unpriced = record.cost_unpriced
    if rows:
        total_tokens = sum(row.llm_tokens for row in rows)
        cost, cost_unpriced = _cost_totals(rows)
        total_cost = format(cost, "f")
        symbols = [_symbol_row(row) for row in rows]
    else:
        symbols = [
            _symbol_row(
                ScanSymbolRecord(
                    scan_id=record.scan_id,
                    symbol=record.symbol,
                    stage=str(payload.get("symbol_stage") or record.status),
                    started_at=record.started_at,
                    finished_at=record.finished_at or None,
                    error_code=record.error_code,
                    report_id=record.report_id,
                    data_quality=record.data_quality,
                    error_message=record.error_message,
                    llm_status=record.llm_status,
                    llm_tokens=record.llm_tokens,
                    cost_usd=record.cost_usd,
                    cost_unpriced=record.cost_unpriced,
                )
            )
        ]
    payload.update(
        {
            "batch_id": record.batch_id or record.scan_id,
            "symbols": symbols,
            "symbols_total": len(symbols),
            "symbols_completed": sum(1 for item in symbols if item["stage"] == "DONE"),
            "symbols_degraded": sum(1 for item in symbols if item["stage"] == "DEGRADED"),
            "symbols_failed": sum(1 for item in symbols if item["stage"] == "FAILED"),
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "cost_unpriced": cost_unpriced,
            "report_links": _report_links(rows)
            if rows
            else (
                [
                    {
                        "symbol": record.symbol,
                        "report_id": record.report_id,
                        "href": f"/reports/{record.report_id}",
                    }
                ]
                if record.report_id
                else []
            ),
            "delivery": _delivery(),
        }
    )
    return payload


def _record_stage(
    container: Any,
    scan_id: str,
    symbol: str,
    started_at: str,
) -> Any:
    def _stage(name: str) -> None:
        container.store.upsert_scan_symbol(
            ScanSymbolRecord(
                scan_id=scan_id,
                symbol=symbol,
                stage=name,
                started_at=started_at,
                finished_at=None,
                error_code=None,
                report_id=None,
                data_quality=None,
            )
        )

    return _stage


def _run_metrics_scan(request: Request, payload: ScanCreateRequest) -> dict[str, Any]:
    container = request.app.state.container
    ledger = JobLedger(store=container.store, clock=container.clock)
    scan_id = _scan_id(container.clock.now())
    assert payload.symbol is not None
    claim = ledger.claim_scan(
        trigger_source="MANUAL",
        owner=scan_id,
        run_id=scan_id,
        source=payload.source,
        metrics_only=payload.metrics_only,
        provider=payload.provider,
        symbol=payload.symbol,
    )
    if not claim.claimed:
        conflict = ScanAlreadyRunning(running_scan_id=claim.run_id)
        raise ApiError(
            conflict.code,
            conflict.message,
            details=_scan_error_details(conflict),
        )
    assert claim.run is not None
    assert payload.symbol is not None
    started_at = utc_timestamp(container.clock.now())
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=scan_id,
            symbol=payload.symbol,
            stage="QUEUED",
            started_at=started_at,
            finished_at=None,
            error_code=None,
            report_id=None,
            data_quality=None,
        )
    )
    heartbeat_stop = threading.Event()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(_SCAN_HEARTBEAT_SECONDS):
            try:
                ledger.heartbeat(claim.run.run_id, owner=claim.run.lease_owner)  # type: ignore[union-attr]
                ledger.acquire_scan_lock(claim.run.lease_owner)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - never stop the scan for a heartbeat
                import structlog

                structlog.get_logger(__name__).warning(
                    "scan_heartbeat_failed",
                    run_id=claim.run.run_id,  # type: ignore[union-attr]
                    owner=claim.run.lease_owner,  # type: ignore[union-attr]
                    error=str(exc),
                )

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    try:
        # Resolve the persisted LLM overlay before the scan reads the provider
        # name/model, so a save made through the API or CLI applies to this scan
        # without a restart.
        container.refresh_llm_settings()
        run = ScanService(
            container=container,
            lock=container.scan_lock,
            on_stage=_record_stage(container, scan_id, payload.symbol, started_at),
        ).run(
            ScanRequest(
                symbol=payload.symbol,
                period=payload.period,
                source=payload.source,
                metrics_only=payload.metrics_only,
                start=payload.start,
                end=payload.end,
                provider=payload.provider,
                batch_id=scan_id,
            ),
            scan_id=scan_id,
        )
    except ScanError as exc:
        ledger.finish(claim.run.run_id, "FAILED", error_code=exc.code, owner=claim.run.lease_owner)
        container.store.upsert_scan_symbol(
            ScanSymbolRecord(
                scan_id=scan_id,
                symbol=payload.symbol,
                stage="FAILED",
                started_at=started_at,
                finished_at=utc_timestamp(container.clock.now()),
                error_code=exc.code,
                error_message=exc.message,
                failed_stage=_failure_stage(exc, exc.code),
                report_id=None,
                data_quality=None,
                cost_usd="0",
                cost_unpriced=False,
            )
        )
        raise ApiError(exc.code, exc.message, details=_scan_error_details(exc)) from exc
    except BaseException as exc:
        ledger.finish(
            claim.run.run_id,
            "FAILED",
            error_code="SCAN_FAILED",
            owner=claim.run.lease_owner,
        )
        # A non-ScanError (including a cancellation) must still leave a
        # terminal checkpoint row; otherwise the symbol renders as running.
        container.store.upsert_scan_symbol(
            ScanSymbolRecord(
                scan_id=scan_id,
                symbol=payload.symbol,
                stage="FAILED",
                started_at=started_at,
                finished_at=utc_timestamp(container.clock.now()),
                error_code="SCAN_FAILED",
                error_message=str(exc),
                failed_stage=_failure_stage(exc, "SCAN_FAILED"),
                report_id=None,
                data_quality=None,
                cost_usd="0",
                cost_unpriced=False,
            )
        )
        raise
    else:
        container.store.upsert_scan_symbol(
            ScanSymbolRecord(
                scan_id=scan_id,
                symbol=payload.symbol,
                stage=run.symbol_stage,
                started_at=started_at,
                finished_at=run.scan.finished_at,
                error_code=None,
                report_id=run.report.report_id,
                data_quality=run.report.data_quality,
                llm_status=(
                    str(run.llm_state.get("validation_status"))
                    if run.llm_state.get("configured")
                    else None
                ),
                llm_tokens=run.llm_tokens,
                cost_usd=run.cost_usd,
                cost_unpriced=run.cost_unpriced,
            )
        )
        ledger.finish(claim.run.run_id, "DONE", owner=claim.run.lease_owner)
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
        ledger.release_scan_lock(claim.run.lease_owner)
    detail = _scan_detail_payload(container, run.scan)
    return success_envelope(
        {
            "scan": public_scan_payload(run.scan),
            "report": report_summary(run.report),
            "report_id": run.report.report_id,
            "symbols": detail["symbols"],
            "report_links": detail["report_links"],
            "delivery": detail["delivery"],
        },
        request_id=new_request_id(),
        generated_at=run.report.generated_at,
        provenance="CALC",
        data_quality=run.report.data_quality,
    )


def _run_batch_scan(request: Request, payload: ScanCreateRequest) -> dict[str, Any]:
    """Run one manual batch over every starred symbol through the durable claim."""
    container = request.app.state.container
    runner = ScheduledScanRunner(container=container)
    try:
        outcome = runner.run_manual(
            period=payload.period,
            source=payload.source,
            metrics_only=payload.metrics_only,
            provider=payload.provider,
            start=payload.start,
            end=payload.end,
        )
    except ScanAlreadyRunning as exc:
        raise ApiError(
            exc.code,
            exc.message,
            details=_scan_error_details(exc),
        ) from exc
    if outcome.reason == "no_starred_symbols":
        raise ApiError(
            "VALIDATION_FAILED",
            "No starred symbols exist; add at least one symbol before running a scan.",
        )
    assert outcome.run is not None
    data = _batch_scan_payload(container, outcome.run, period=payload.period)
    data["symbols_total"] = outcome.symbols_total or data["symbols_total"]
    data["degraded_symbols"] = list(outcome.degraded_symbols)
    return success_envelope(
        data,
        request_id=new_request_id(),
        generated_at=outcome.run.finished_at or outcome.run.started_at,
        provenance="CALC",
        data_quality=str(data["data_quality"]),
    )


@router.post("/api/v1/scans", status_code=201)
def create_scan(
    payload: ScanCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Run one manual scan; omitting ``symbol`` scans all starred symbols.

    ``Idempotency-Key`` is mandatory for the mutation surface. A replay of the
    same key + payload returns the stored response with
    ``meta.idempotent_replay=true``; a different payload is a 409 conflict.
    """
    container = request.app.state.container
    if payload.fixture_path is not None:
        if payload.symbol is None:
            raise ApiError(
                "VALIDATION_FAILED",
                "fixture_path scans require an explicit symbol.",
                details=[
                    {
                        "field": "symbol",
                        "code": "missing",
                        "message": "fixture_path scans require an explicit symbol.",
                    }
                ],
            )
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
        ttl=_IDEMPOTENCY_LEASE,
    )
    if not container.store.reserve_idempotency(reservation):
        existing = container.store.get_idempotency(scope=scoped_key, key=key)
        if (
            existing is not None
            and existing.expires_at <= now
            and container.store.reserve_idempotency(reservation)
        ):
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

    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(_IDEMPOTENCY_HEARTBEAT.total_seconds()):
            with suppress(Exception):
                container.store.renew_idempotency(
                    scope=scoped_key,
                    key=key,
                    expires_at=utc_timestamp(container.clock.now() + _IDEMPOTENCY_LEASE),
                )

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        envelope = (
            _run_metrics_scan(request, payload)
            if payload.symbol is not None
            else _run_batch_scan(request, payload)
        )
    except BaseException:
        stop_heartbeat.set()
        beat.join(timeout=1)
        container.store.delete_idempotency(scope=scoped_key, key=key)
        raise
    stop_heartbeat.set()
    beat.join(timeout=1)
    _complete_idempotency(request, key=key, request_hash=request_hash, envelope=envelope)
    return JSONResponse(status_code=201, content=envelope)


@router.get("/api/v1/scans")
def list_scans(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List recent scans per batch (newest first) for ``oft scan history``.

    Each item is one batch run, so the history shows the batch status, symbol
    counts (including failed symbols), cost, and delivery state together.
    """
    container = request.app.state.container
    runs = container.store.list_job_runs("scan", limit=limit, offset=offset)
    total = container.store.count_job_runs("scan")
    items = [_batch_scan_payload(container, run) for run in runs]
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
    record = get_scan(container, scan_id)
    if record is not None:
        detail = _scan_detail_payload(container, record)
        return success_envelope(
            detail,
            request_id=new_request_id(),
            generated_at=record.finished_at,
            provenance="CALC",
            data_quality=record.data_quality,
        )
    run = container.store.get_job_run(scan_id)
    if run is not None and run.job_type == "scan":
        data = _batch_scan_payload(container, run)
        return success_envelope(
            data,
            request_id=new_request_id(),
            generated_at=run.finished_at or run.started_at,
            provenance="CALC",
            data_quality=str(data["data_quality"]),
        )
    raise ApiError("NOT_FOUND", f"Scan {scan_id} does not exist.")
