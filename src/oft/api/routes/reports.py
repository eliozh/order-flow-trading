"""Report routes: CALC-only reports produced by metrics-only scans."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.scan_service import report_payload, report_summary

router = APIRouter()


def _report_or_404(request: Request, report_id: str) -> Any:
    record = request.app.state.container.store.get_report(report_id)
    if record is None:
        raise ApiError("NOT_FOUND", f"Report {report_id} does not exist.")
    return record


@router.get("/api/v1/reports")
def list_reports(
    request: Request,
    scan_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List recent reports, optionally filtered to one scan."""
    store = request.app.state.container.store
    records = store.list_reports(scan_id=scan_id, limit=limit, offset=offset)
    total = store.count_reports(scan_id=scan_id)
    items = [report_summary(record) for record in records]
    has_next = offset + len(items) < total
    return success_envelope(
        {"items": items, "total": total, "scan_id": scan_id},
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


@router.get("/api/v1/reports/{report_id}")
def read_report(
    request: Request,
    report_id: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    """Return one frozen CALC report: metric manifest, snapshot, and metrics."""
    record = _report_or_404(request, report_id)
    payload = report_payload(record)
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=record.generated_at,
        provenance="CALC",
        data_quality=record.data_quality,
    )
