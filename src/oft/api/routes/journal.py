"""Closed-trade journal API: read endpoints plus the mutating advisory review POST."""

from __future__ import annotations

from pathlib import Path as FilePath
from typing import Any, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import FileResponse

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
from oft.api.schemas.journal import (
    Envelope,
    JournalCompareData,
    JournalExportData,
    JournalReviewData,
    JournalReviewRequest,
    JournalStatsData,
    TradeLogListData,
)
from oft.application.journal_service import JournalError, JournalService
from oft.application.review_service import ReviewRejected, ReviewService

router = APIRouter()
_REVIEW_QUALITY = {"FOUND": "COMPLETE", "PARTIAL": "PARTIAL", "MISSING": "MISSING"}


def _journal_service(request: Request) -> JournalService:
    container = request.app.state.container
    return JournalService(container.store, container.clock, container.settings)


def _review_service(request: Request, provider: str | None) -> ReviewService:
    container = request.app.state.container
    # Resolve the provider first: ``provider_for`` refreshes the shared
    # snapshot, so reading ``container.settings`` afterwards gives the one
    # effective snapshot the adapter was built from (no stale-settings/fresh-
    # adapter mixture within a single request).
    resolved_provider = container.provider_for(provider)
    return ReviewService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
        provider=resolved_provider,
        runtime=container.llm_runtime,
        notifications=container.notifications,
    )


def _review_error(exc: ReviewRejected) -> ApiError:
    """Map a review code through the catalog exactly like the management route."""
    code = exc.code if exc.code in ERROR_CATALOG else "CONFLICT"
    details = (
        [{"field": exc.field, "code": exc.code, "message": exc.message}]
        if exc.field is not None
        else []
    )
    return ApiError(code, exc.message, details=details)


def trade_log_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The minimal journal projection shared by the list route and the CLI."""
    return {
        "trade_id": payload.get("trade_id"),
        "position_id": payload.get("position_id"),
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "opened_at": payload.get("opened_at"),
        "closed_at": payload.get("closed_at"),
        "gross_pnl_usd": payload.get("gross_pnl_usd"),
        "fees_usd": payload.get("fees_usd"),
        "funding_usd": payload.get("funding_usd"),
        "net_pnl_usd": payload.get("net_pnl_usd"),
        "return_pct": payload.get("return_pct"),
        "r_multiple": payload.get("r_multiple"),
        "mae_usd": payload.get("mae_usd"),
        "mfe_usd": payload.get("mfe_usd"),
        "outcome_tags": payload.get("outcome_tags") or [],
        "data_quality": payload.get("data_quality"),
    }


@router.get("/api/v1/journal", response_model=Envelope[TradeLogListData])
def list_journal(
    request: Request,
    symbol: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    resolved_symbol = symbol.upper() if symbol else None
    service = _journal_service(request)
    listed = service.list(symbol=resolved_symbol, limit=limit, offset=offset)
    items = [trade_log_summary(payload) for payload in listed["items"]]
    total = int(listed["total"])
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


@router.get("/api/v1/journal/stats", response_model=Envelope[JournalStatsData])
def journal_stats(
    request: Request,
    period: str | None = Query(default=None, max_length=16),
    symbol: str | None = Query(default=None, max_length=24),
) -> dict[str, Any]:
    """Deterministic PnL/win-rate/expectancy/avg-R statistics with exclusions."""
    try:
        stats = _journal_service(request).stats(period=period, symbol=symbol)
    except JournalError as exc:
        raise ApiError(exc.code, exc.message) from exc
    return success_envelope(
        stats,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=str(stats.get("data_quality") or "MISSING"),
    )


@router.get("/api/v1/journal/export", response_model=Envelope[JournalExportData])
def export_journal(
    request: Request,
    format: Literal["csv", "json"] = Query(default="csv"),
    period: str | None = Query(default=None, max_length=16),
    symbol: str | None = Query(default=None, max_length=24),
) -> dict[str, Any]:
    """Write a CSV/JSON export under the data directory and report its path."""
    try:
        result = _journal_service(request).export(fmt=format, period=period, symbol=symbol)
    except JournalError as exc:
        raise ApiError(exc.code, exc.message) from exc
    return success_envelope(
        result,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=str(result.get("data_quality") or "MISSING"),
    )


_EXPORT_MEDIA_TYPES = {"csv": "text/csv", "json": "application/json"}


@router.get("/api/v1/journal/export/file")
def export_journal_file(
    request: Request,
    format: Literal["csv", "json"] = Query(default="csv"),
    period: str | None = Query(default=None, max_length=16),
    symbol: str | None = Query(default=None, max_length=24),
) -> FileResponse:
    """Generate an export and return the just-written file for download.

    The route accepts only query parameters (format/period/symbol); it never
    accepts a path, so a caller cannot read an arbitrary file. CSV formula
    neutralization happens in ``JournalService.export`` before the file exists.
    """
    try:
        result = _journal_service(request).export(fmt=format, period=period, symbol=symbol)
    except JournalError as exc:
        raise ApiError(exc.code, exc.message) from exc
    path = FilePath(str(result["path"]))
    return FileResponse(
        path,
        media_type=_EXPORT_MEDIA_TYPES[str(result["format"])],
        filename=path.name,
    )


@router.get("/api/v1/journal/compare/{trade_id}", response_model=Envelope[JournalCompareData])
def journal_compare(
    request: Request,
    trade_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """The signal-vs-outcome comparison for one closed trade."""
    try:
        comparison = _journal_service(request).compare(trade_id)
    except JournalError as exc:
        raise ApiError(exc.code, exc.message) from exc
    return success_envelope(
        comparison,
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality=str(comparison.get("data_quality") or "MISSING"),
    )


@router.post(
    "/api/v1/journal/{trade_id}/review",
    response_model=Envelope[JournalReviewData],
)
def review_trade(
    request: Request,
    trade_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
    refresh: bool = Query(default=False),
    provider: str | None = Query(default=None, max_length=32),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Persist one advisory LLM review and link it from the trade log."""
    key = require_idempotency_key(idempotency_key)
    payload = JournalReviewRequest(refresh=refresh, provider=provider)
    payload_hash = compute_request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
    if replay is not None:
        return replay
    try:
        result = _review_service(request, payload.provider).review(
            trade_id,
            refresh=refresh,
            idempotency_key=key,
            provider_name=payload.provider,
        )
    except ReviewRejected as exc:
        release(request, key=key)
        raise _review_error(exc) from exc
    except BaseException:
        release(request, key=key)
        raise
    envelope = success_envelope(
        result.to_contract(),
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality=_REVIEW_QUALITY[result.status],
        idempotent_replay=result.idempotent_replay,
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=200)
    return envelope


@router.get(
    "/api/v1/journal/{trade_id}/review",
    response_model=Envelope[JournalReviewData],
)
def read_trade_review(
    request: Request,
    trade_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """The linked advisory review (provider-free; named MISSING/PARTIAL states)."""
    try:
        read = _review_service(request, None).read(trade_id)
    except ReviewRejected as exc:
        raise _review_error(exc) from exc
    return success_envelope(
        read.to_contract(),
        request_id=new_request_id(),
        provenance="MIXED",
        data_quality=_REVIEW_QUALITY[read.status],
    )


@router.get("/api/v1/journal/{trade_id}", response_model=Envelope[dict[str, Any]])
def read_journal_trade(
    request: Request,
    trade_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    try:
        payload = _journal_service(request).show(trade_id)
    except JournalError as exc:
        raise ApiError(exc.code, exc.message) from exc
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=str(payload.get("closed_at") or ""),
        provenance="CALC",
        data_quality=str((payload.get("data_quality") or {}).get("status", "COMPLETE")),
    )


__all__ = ["router", "trade_log_summary"]
