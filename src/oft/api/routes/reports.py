"""Report routes: frozen scan reports with CALC metrics and an optional LLM band."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.envelope import Envelope
from oft.application.chart_service import (
    SUPPORTED_CHART_PERIODS,
    derive_overlays,
    resolve_chart,
)
from oft.application.report_export import (
    provenance_counts,
    redact_report_payload,
    render_report_json,
    render_report_markdown,
)
from oft.application.scan_service import report_payload, report_summary
from oft.ports.store import ReportRecord

router = APIRouter()


def _report_or_404(request: Request, report_id: str) -> ReportRecord:
    record: ReportRecord | None = request.app.state.container.store.get_report(report_id)
    if record is None:
        raise ApiError("NOT_FOUND", f"Report {report_id} does not exist.")
    return record


@router.get("/api/v1/reports")
def list_reports(
    request: Request,
    scan_id: str | None = Query(default=None, min_length=1, max_length=64),
    symbol: str | None = Query(
        default=None, min_length=2, max_length=24, pattern=r"^[A-Z0-9]{2,24}$"
    ),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List recent reports, optionally filtered to one scan or one symbol."""
    store = request.app.state.container.store
    if symbol is None:
        records = store.list_reports(scan_id=scan_id, limit=limit, offset=offset)
        total = store.count_reports(scan_id=scan_id)
    else:
        # The symbol filter is additive and SQL-bounded: ordering, scan scoping,
        # and LIMIT/OFFSET run in the query, so the route never materializes
        # every frozen report just to slice one page.
        records = store.list_reports_for_symbol_page(
            symbol, scan_id=scan_id, limit=limit, offset=offset
        )
        total = store.count_reports_for_symbol(symbol, scan_id=scan_id)
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
    """Return one frozen report: metric manifest, snapshot, metrics, and LLM state.

    Provenance is derived from the same ``provenance_counts`` the export uses,
    so the detail view and the export agree (``MIXED`` when an LLM block is
    present, otherwise ``CALC``).
    """
    record = _report_or_404(request, report_id)
    payload = report_payload(record)
    counts = provenance_counts(payload)
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=record.generated_at,
        provenance="MIXED" if counts["llm"] > 0 else "CALC",
        data_quality=record.data_quality,
    )


@router.get(
    "/api/v1/reports/{report_id}/export",
    response_model=None,
    responses={
        200: {
            "description": "Redacted report export attachment.",
            # ``model`` registers the shared envelope (and its meta) in
            # components and references it by ``$ref``; inlining
            # ``model_json_schema()`` would leave ``#/$defs`` pointers dangling
            # at the OpenAPI document root.
            "model": Envelope[dict[str, Any]],
            "content": {"text/markdown": {"schema": {"type": "string"}}},
            "headers": {
                "Content-Disposition": {"schema": {"type": "string"}},
                "X-OFT-Calc-Blocks": {"schema": {"type": "string"}},
                "X-OFT-Llm-Blocks": {"schema": {"type": "string"}},
            },
        },
        404: {"description": "NOT_FOUND — report does not exist."},
        422: {"description": "VALIDATION_FAILED — invalid format or report_id."},
    },
)
def export_report(
    request: Request,
    report_id: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    format: Literal["md", "json"] = Query(default="md"),
) -> Response:
    """Return one frozen report as a redacted Markdown/JSON attachment.

    The route accepts only ``report_id`` (path) and ``format`` (query); it never
    accepts a caller filesystem path. Both formats reuse ``report_payload`` so
    the frozen metrics, provenance, and data-quality stamps cannot drift, then
    redact secret/prompt keys before rendering. The JSON envelope reports
    ``MIXED`` provenance when the payload has an LLM block, else ``CALC``.
    """
    record = _report_or_404(request, report_id)
    payload = redact_report_payload(report_payload(record))
    counts = provenance_counts(payload)
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", report_id)
    headers = {
        "Content-Disposition": f'attachment; filename="report-{safe_id}.{format}"',
        "X-OFT-Calc-Blocks": str(counts["calc"]),
        "X-OFT-Llm-Blocks": str(counts["llm"]),
    }
    if format == "json":
        envelope = success_envelope(
            payload,
            request_id=new_request_id(),
            generated_at=record.generated_at,
            provenance="MIXED" if counts["llm"] > 0 else "CALC",
            data_quality=record.data_quality,
        )
        return Response(
            content=render_report_json(envelope),
            media_type="application/json",
            headers=headers,
        )
    return Response(
        content=render_report_markdown(payload),
        media_type="text/markdown",
        headers=headers,
    )


@router.get("/api/v1/reports/{report_id}/chart")
def read_report_chart(
    request: Request,
    report_id: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return one chart for this report: a published period or an honest MISSING.

    An omitted ``period`` means the report's native timeframe. Explicit values
    must be one of ``15m``/``1h``/``4h`` or that native timeframe; anything else
    is a validation failure naming the allowed values.

    Overlays (opportunity zones/trigger lines and position markers) are derived
    from this report's linked records, independent of ``period``. Switching the
    timeframe changes only the drawn series; it never invents overlays.
    """
    record = _report_or_404(request, report_id)
    requested = period if period is not None else record.timeframe
    if requested != record.timeframe and requested not in SUPPORTED_CHART_PERIODS:
        allowed = ", ".join(dict.fromkeys((*SUPPORTED_CHART_PERIODS, record.timeframe)))
        raise ApiError(
            "VALIDATION_FAILED",
            f"period must be one of {allowed}; got {requested!r}.",
        )
    store = request.app.state.container.store
    # Resolve first: a MISSING chart draws no series, so it must not read (or
    # announce) overlays that cannot be drawn.
    payload = resolve_chart(record, requested, store)
    if payload["status"] == "AVAILABLE":
        zones, markers, truncated = derive_overlays(record, store)
        payload = {
            **payload,
            "zones": zones,
            "markers": markers,
            "overlays_truncated": truncated,
        }
    generated_at = payload["generated_at"] or record.generated_at
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=generated_at,
        provenance="CALC",
        data_quality=payload["data_quality"],
    )
