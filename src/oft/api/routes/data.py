"""Backfill routes: estimate, create, and per-run status (ADR-0003 envelope)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.backfill_service import (
    MAX_BACKFILL_DAYS,
    BackfillAlreadyRunning,
    BackfillResult,
    BackfillValidationError,
    SizeEstimate,
)
from oft.application.gap_detection import detect_backfill_gaps
from oft.domain.time import utc_timestamp
from oft.ports.archive import ArchiveError, ChecksumMismatchError, UnsafeArchiveError
from oft.ports.store import BackfillManifestRow, BackfillRunRecord

router = APIRouter()

_DEFAULT_DATA_TYPE = "aggTrades"


class BackfillCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    start: date
    end: date
    data_type: str = Field(default=_DEFAULT_DATA_TYPE, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    confirm: bool = False

    @model_validator(mode="after")
    def _validate_range(self) -> BackfillCreateRequest:
        if self.end < self.start:
            raise ValueError("start must not be after end")
        if (self.end - self.start).days + 1 > MAX_BACKFILL_DAYS:
            raise ValueError(f"range exceeds the {MAX_BACKFILL_DAYS}-day limit")
        return self


def _estimate_payload(estimate: SizeEstimate) -> dict[str, Any]:
    return estimate.to_dict()


def _run_payload(run: BackfillRunRecord) -> dict[str, Any]:
    return asdict(run)


def _file_payload(row: BackfillManifestRow) -> dict[str, Any]:
    return asdict(row)


def _quality(container: Any, run: BackfillRunRecord) -> str:
    gaps = container.store.list_data_gaps(symbol=run.symbol, data_type=run.data_type)
    unresolved = [gap for gap in gaps if gap.resolved_at is None]
    if unresolved and run.status == "COMPLETE":
        return "PARTIAL"
    if run.status == "COMPLETE":
        return "COMPLETE"
    if run.status == "PARTIAL":
        return "PARTIAL"
    return "MISSING"


def _gap_payload(gap: Any) -> dict[str, Any]:
    return {
        "gap_id": gap.gap_id,
        "symbol": gap.symbol,
        "data_type": gap.data_type,
        "start": gap.start,
        "end": gap.end,
        "reason": gap.reason,
        "severity": gap.severity.value,
        "detected_at": gap.detected_at,
        "source": gap.source,
        "resolved_at": gap.resolved_at,
        "resolution": gap.resolution,
        "run_id": gap.run_id,
    }


def _status_data(container: Any, run: BackfillRunRecord) -> dict[str, Any]:
    files = tuple(
        row
        for row in container.store.list_backfill_manifest(
            symbol=run.symbol, data_type=run.data_type
        )
        if run.start_date <= row.date <= run.end_date
    )
    gaps = container.store.list_data_gaps(symbol=run.symbol, data_type=run.data_type)
    return {
        "run": _run_payload(run),
        "files": [_file_payload(row) for row in files],
        "gaps": [_gap_payload(gap) for gap in gaps],
    }


def _created_data(container: Any, result: BackfillResult) -> dict[str, Any]:
    return {
        "estimate": _estimate_payload(result.estimate),
        "run": _run_payload(result.run),
        "files": [_file_payload(row) for row in result.files],
    }


@router.post("/api/v1/backfill")
def create_backfill(
    payload: BackfillCreateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Estimate a backfill, or run it when ``confirm`` is true."""
    container = request.app.state.container
    service = container.backfill
    # Fail fast before any I/O when the symbol is not in the slice-02 registry.
    if container.store.get_symbol(payload.symbol) is None:
        raise ApiError(
            "VALIDATION_FAILED",
            f"Symbol {payload.symbol} is not in the symbol registry.",
        )
    try:
        estimate = service.estimate(
            symbol=payload.symbol,
            data_type=payload.data_type,
            start=payload.start,
            end=payload.end,
        )
        if not payload.confirm:
            response.status_code = 200
            return success_envelope(
                {
                    "estimate": _estimate_payload(estimate),
                    "run": None,
                    "files": [],
                },
                request_id=new_request_id(),
                data_quality="COMPLETE" if estimate.files else "MISSING",
            )
        result = service.run(
            symbol=payload.symbol,
            data_type=payload.data_type,
            start=payload.start,
            end=payload.end,
        )
    except BackfillAlreadyRunning as exc:
        raise ApiError("BACKFILL_ALREADY_RUNNING", str(exc)) from exc
    except ChecksumMismatchError as exc:
        raise ApiError("BACKFILL_CHECKSUM_MISMATCH", str(exc)) from exc
    except UnsafeArchiveError as exc:
        raise ApiError("VALIDATION_FAILED", str(exc)) from exc
    except BackfillValidationError as exc:
        raise ApiError("VALIDATION_FAILED", str(exc)) from exc
    except ArchiveError as exc:
        raise ApiError("DATA_UNAVAILABLE", str(exc)) from exc

    response.status_code = 201
    return success_envelope(
        _created_data(container, result),
        request_id=new_request_id(),
        generated_at=result.run.finished_at,
        data_quality=_quality(container, result.run),
    )


@router.get("/api/v1/backfill/latest")
def latest_backfill(request: Request) -> dict[str, Any]:
    """Status of the most recent run (used by ``oft backfill status``)."""
    container = request.app.state.container
    run = container.store.latest_backfill_run()
    if run is None:
        raise ApiError("NOT_FOUND", "No backfill run exists.")
    return success_envelope(
        _status_data(container, run),
        request_id=new_request_id(),
        generated_at=run.finished_at or run.started_at,
        data_quality=_quality(container, run),
    )


@router.get("/api/v1/backfill/{run_id}")
def read_backfill(run_id: str, request: Request) -> dict[str, Any]:
    """Return one run plus its per-file manifest rows and gaps."""
    container = request.app.state.container
    run = container.store.get_backfill_run(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"Backfill run {run_id} does not exist.")
    return success_envelope(
        _status_data(container, run),
        request_id=new_request_id(),
        generated_at=run.finished_at or run.started_at,
        data_quality=_quality(container, run),
    )


@router.get("/api/v1/data/quality")
def data_quality(
    request: Request,
    symbol: Annotated[str, Query(min_length=2, max_length=24, pattern=r"^[A-Z0-9]{2,24}$")],
    data_type: Annotated[
        str, Query(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    ] = _DEFAULT_DATA_TYPE,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Detect and return the data-quality window for a symbol (ADR-0003)."""
    container = request.app.state.container
    clock = container.clock
    resolved_end = end or clock.now().date()
    resolved_start = start or (resolved_end - timedelta(days=6))
    if resolved_end < resolved_start:
        raise ApiError("VALIDATION_FAILED", "start must not be after end")
    if (resolved_end - resolved_start).days + 1 > MAX_BACKFILL_DAYS:
        raise ApiError(
            "VALIDATION_FAILED",
            f"range exceeds the {MAX_BACKFILL_DAYS}-day limit",
        )
    result = detect_backfill_gaps(
        container.settings,
        container.store,
        symbol=symbol,
        data_type=data_type,
        start=resolved_start,
        end=resolved_end,
        clock=clock,
    )
    return success_envelope(
        {
            "symbol": symbol,
            "data_type": data_type,
            "start": result.start,
            "end": result.end,
            "expected_days": result.expected_days,
            "present_days": result.present_days,
            "status": result.quality.status.value,
            "reasons": list(result.quality.reasons),
            "gaps": [_gap_payload(gap) for gap in result.quality.gaps],
        },
        request_id=new_request_id(),
        generated_at=utc_timestamp(clock.now()),
        provenance="CALC",
        data_quality=result.quality.status.value,
    )
