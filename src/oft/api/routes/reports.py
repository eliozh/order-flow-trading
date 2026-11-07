"""Report routes: CALC-only reports produced by metrics-only scans."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

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
) -> dict[str, Any]:
    """List recent reports, optionally filtered to one scan."""
    records = request.app.state.container.store.list_reports(scan_id=scan_id, limit=limit)
    items = [report_summary(record) for record in records]
    return success_envelope(
        {"items": items, "total": len(items), "scan_id": scan_id},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
        pagination={
            "limit": limit,
            "cursor": None,
            "has_next": len(items) == limit,
            "next_cursor": str(limit) if len(items) == limit else None,
            "total": len(items),
        },
    )


@router.get("/api/v1/reports/{report_id}")
def read_report(report_id: str, request: Request) -> dict[str, Any]:
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
