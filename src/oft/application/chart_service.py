"""Resolve one chart payload from frozen reports (slice 39).

Resolution rule
---------------
A frozen report already *is* published bar and metric data, so the chart panel
never derives a series:

* the report's native period renders the requested report's frozen
  ``snapshot.bars`` and ``metrics`` verbatim;
* any other period renders the newest report (by ``generated_at``, then
  ``report_id``) for the same symbol whose ``timeframe`` equals the requested
  period, again verbatim.

There is no reconstruction, resampling, upsampling, or interpolation. When no
published report exists for the requested period the payload is ``MISSING``
with an explicit reason, so the UI can explain the absence instead of drawing
an invented series. A frozen document whose JSON is malformed, or whose root is
not an object, is treated as storage corruption and also resolves to
``MISSING`` rather than an unhandled server error.

The module is pure apart from the injected ``StorePort`` read. The symbol's
reports are read exactly once per request and grouped by timeframe, so both the
available-period list and the switched-period source share one snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from oft.ports.store import ReportRecord, StorePort

#: Periods the timeframe switch may offer beyond the report's native period.
SUPPORTED_CHART_PERIODS: tuple[str, ...] = ("15m", "1h", "4h")

_STATUS_AVAILABLE = "AVAILABLE"
_STATUS_MISSING = "MISSING"


class _CorruptReportError(ValueError):
    """A frozen report document is unreadable or has a non-object root."""

    def __init__(self, report_id: str) -> None:
        super().__init__(f"frozen report {report_id} is unreadable")
        self.report_id = report_id


def _recency(report: ReportRecord) -> tuple[str, str]:
    return (report.generated_at, report.report_id)


def _newest_by_timeframe(reports: Sequence[ReportRecord]) -> dict[str, ReportRecord]:
    """The newest report per exact timeframe, keyed by ``timeframe``.

    ``generated_at`` is the uniform UTC format, so the lexical maximum equals
    the chronological maximum; ``report_id`` breaks a same-instant tie.
    """
    newest: dict[str, ReportRecord] = {}
    for report in reports:
        current = newest.get(report.timeframe)
        if current is None or _recency(report) > _recency(current):
            newest[report.timeframe] = report
    return newest


def available_periods(report: ReportRecord, published: Mapping[str, ReportRecord]) -> list[str]:
    """Native period first, then 15m/1h/4h that have a published report.

    The native period is always present. A switch period is listed only when a
    report with that exact timeframe exists for the symbol, and every period
    appears once.
    """
    periods: list[str] = [report.timeframe]
    for period in SUPPORTED_CHART_PERIODS:
        if period in periods:
            continue
        if period in published:
            periods.append(period)
    return periods


def _frozen(report: ReportRecord) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The report's frozen bars and metrics, or ``_CorruptReportError``.

    Both stored documents must parse as JSON objects. The nested ``bars`` list
    is guarded separately so a well-formed root with an unexpected nested shape
    still yields an empty series instead of an exception.
    """
    try:
        snapshot = json.loads(report.snapshot_json)
        metrics = json.loads(report.metrics_json)
    except (TypeError, ValueError) as exc:
        raise _CorruptReportError(report.report_id) from exc
    if not isinstance(snapshot, dict) or not isinstance(metrics, dict):
        raise _CorruptReportError(report.report_id)
    bars = snapshot.get("bars", [])
    if not isinstance(bars, list):
        bars = []
    return bars, metrics


def _missing_payload(
    report: ReportRecord,
    native_period: str,
    requested_period: str,
    periods: list[str],
    reason: str,
) -> dict[str, Any]:
    """An explicit MISSING payload: no bars, no metrics, and a reason."""
    return {
        "report_id": report.report_id,
        "symbol": report.symbol,
        "native_period": native_period,
        "requested_period": requested_period,
        "status": _STATUS_MISSING,
        "available_periods": periods,
        "source_report_id": None,
        "generated_at": None,
        "bars": [],
        "metrics": None,
        "gaps": [],
        "data_quality": "MISSING",
        "quality_reasons": [],
        "reason": reason,
    }


def resolve_chart(report: ReportRecord, requested_period: str, store: StorePort) -> dict[str, Any]:
    """Build the chart payload for ``requested_period`` without inventing data."""
    native_period = report.timeframe
    # One read per request: group the symbol's reports by timeframe first, then
    # serve both the available-period list and the switched-period source.
    published = _newest_by_timeframe(store.list_reports_for_symbol(report.symbol))
    periods = available_periods(report, published)
    source = report if requested_period == native_period else published.get(requested_period)
    if source is None:
        reason = (
            f"No published {requested_period} bars or metrics exist for {report.symbol}. "
            "The chart never upsamples or interpolates from another period."
        )
        return _missing_payload(report, native_period, requested_period, periods, reason)

    try:
        bars, metrics = _frozen(source)
    except _CorruptReportError:
        reason = (
            f"The frozen report data for {source.report_id} is unreadable "
            "(storage corruption). The chart never draws from unreadable, invented, "
            "or interpolated values."
        )
        return _missing_payload(report, native_period, requested_period, periods, reason)

    gaps = metrics.get("gaps", [])
    if not isinstance(gaps, list):
        gaps = []
    quality_reasons = metrics.get("quality_reasons", [])
    if not isinstance(quality_reasons, list):
        quality_reasons = []
    return {
        "report_id": report.report_id,
        "symbol": report.symbol,
        "native_period": native_period,
        "requested_period": requested_period,
        "status": _STATUS_AVAILABLE,
        "available_periods": periods,
        "source_report_id": source.report_id,
        "generated_at": source.generated_at,
        "bars": bars,
        "metrics": metrics,
        "gaps": gaps,
        "data_quality": source.data_quality,
        "quality_reasons": quality_reasons,
        "reason": None,
    }
