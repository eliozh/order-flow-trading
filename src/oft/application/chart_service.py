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
an invented series. A frozen document whose JSON is malformed, whose root is
not an object, or whose required nested structure is missing or the wrong shape
is treated as storage corruption and also resolves to ``MISSING`` rather than an
unhandled server error or a partially corrupt ``AVAILABLE`` payload.

Reads are bounded: one header projection lists the newest report per timeframe
(four small columns, no frozen documents), then switching costs *at most* one
full ``get_report`` source read; it costs none when the period is unpublished
or the published source vanished before it could be read. The native period
costs none because the requested report is already loaded by the route.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from oft.ports.store import ReportHeader, ReportRecord, StorePort

#: Periods the timeframe switch may offer beyond the report's native period.
SUPPORTED_CHART_PERIODS: tuple[str, ...] = ("15m", "1h", "4h")

_STATUS_AVAILABLE = "AVAILABLE"
_STATUS_MISSING = "MISSING"


class _CorruptReportError(ValueError):
    """A frozen report document is unreadable or structurally incomplete.

    ``part`` names the frozen part that failed validation (it may be the
    generic ``"the frozen JSON"`` when parsing itself failed) so the MISSING
    reason can identify exactly what is corrupt without exposing the document.
    """

    def __init__(self, report_id: str, part: str) -> None:
        super().__init__(f"frozen report {report_id} is unreadable ({part})")
        self.report_id = report_id
        self.part = part


def _newest_headers(headers: Sequence[ReportHeader]) -> dict[str, ReportHeader]:
    """The newest header per exact timeframe, keyed by ``timeframe``.

    The projection is ordered newest first by ``(generated_at, report_id)``, so
    the first header seen for a timeframe is its newest candidate.
    """
    newest: dict[str, ReportHeader] = {}
    for header in headers:
        newest.setdefault(header.timeframe, header)
    return newest


def available_periods(report: ReportRecord, published: Mapping[str, ReportHeader]) -> list[str]:
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


def _as_mapping_list(value: Any) -> list[Any] | None:
    """The list when every element is a JSON object/Mapping, else ``None``."""
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return value
    return None


def _frozen(report: ReportRecord) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The report's validated frozen bars and metrics, or ``_CorruptReportError``.

    Both stored documents must parse as JSON objects and carry every section the
    chart draws:

    * ``snapshot.bars`` — a list of objects;
    * ``metrics.footprint`` — required, with ``bars`` a list of objects, each bar
      carrying a ``cells`` list of objects;
    * ``metrics.volume_profile`` — required, with ``bins`` a list of objects and a
      section-level ``poc`` (not a per-bin field);
    * ``metrics.gaps`` — required, a list of objects;
    * ``metrics.quality_reasons`` — required, a list of strings.

    These are the sections the metrics contract requires and the writer always
    emits. An absent section, an explicit JSON ``null``, or any other wrong shape
    is corruption, never coerced to an empty list, because an empty list would
    fabricate "no gaps / no reasons" or silently draw an empty series.
    """
    try:
        snapshot = json.loads(report.snapshot_json)
        metrics = json.loads(report.metrics_json)
    except (TypeError, ValueError) as exc:
        raise _CorruptReportError(report.report_id, "the frozen JSON") from exc
    if not isinstance(snapshot, dict):
        raise _CorruptReportError(report.report_id, "snapshot")
    if not isinstance(metrics, dict):
        raise _CorruptReportError(report.report_id, "metrics")

    bars = _as_mapping_list(snapshot.get("bars"))
    if bars is None:
        raise _CorruptReportError(report.report_id, "snapshot.bars")

    footprint = metrics.get("footprint")
    if not isinstance(footprint, Mapping):
        raise _CorruptReportError(report.report_id, "metrics.footprint")
    footprint_bars = _as_mapping_list(footprint.get("bars"))
    if footprint_bars is None:
        raise _CorruptReportError(report.report_id, "metrics.footprint.bars")
    for index, footprint_bar in enumerate(footprint_bars):
        if _as_mapping_list(footprint_bar.get("cells")) is None:
            raise _CorruptReportError(report.report_id, f"metrics.footprint.bars[{index}].cells")

    volume_profile = metrics.get("volume_profile")
    if not isinstance(volume_profile, Mapping):
        raise _CorruptReportError(report.report_id, "metrics.volume_profile")
    bins = _as_mapping_list(volume_profile.get("bins"))
    if bins is None:
        raise _CorruptReportError(report.report_id, "metrics.volume_profile.bins")
    if "poc" not in volume_profile:
        raise _CorruptReportError(report.report_id, "metrics.volume_profile.poc")

    if _as_mapping_list(metrics.get("gaps")) is None:
        raise _CorruptReportError(report.report_id, "metrics.gaps")
    quality_reasons = metrics.get("quality_reasons")
    if not (
        isinstance(quality_reasons, list)
        and all(isinstance(reason, str) for reason in quality_reasons)
    ):
        raise _CorruptReportError(report.report_id, "metrics.quality_reasons")

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
    # One bounded projection read lists the newest report per timeframe; the
    # frozen documents are only fetched for the single switched source below.
    published = _newest_headers(store.list_report_headers_for_symbol(report.symbol))
    periods = available_periods(report, published)
    if requested_period == native_period:
        source = report
    else:
        header = published.get(requested_period)
        if header is None:
            reason = (
                f"No published {requested_period} bars or metrics exist for {report.symbol}. "
                "The chart never upsamples or interpolates from another period."
            )
            return _missing_payload(report, native_period, requested_period, periods, reason)
        loaded = store.get_report(header.report_id)
        if loaded is None:
            reason = (
                f"The published {requested_period} report {header.report_id} for "
                f"{report.symbol} disappeared from storage before it could be read. "
                "The chart never draws from invented or interpolated values."
            )
            return _missing_payload(report, native_period, requested_period, periods, reason)
        source = loaded

    try:
        bars, metrics = _frozen(source)
    except _CorruptReportError as exc:
        reason = (
            f"The frozen report data for {source.report_id} is unreadable "
            f"(storage corruption: {exc.part}). The chart never draws from unreadable, "
            "invented, or interpolated values."
        )
        return _missing_payload(report, native_period, requested_period, periods, reason)

    gaps = metrics["gaps"]
    quality_reasons = metrics["quality_reasons"]
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
