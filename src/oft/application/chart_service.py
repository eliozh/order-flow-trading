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

Overlays are also server-derived, never client-inferred
--------------------------------------------------------
``derive_overlays`` has the store scope each linked read to the route's report
(``symbol`` plus ``source.report_id``; positions through their trigger
linkage). Each query asks SQL for ``_OVERLAY_READ_LIMIT + 1`` rows so an
over-bound result is detected, then is sliced back to ``_OVERLAY_READ_LIMIT``.
It re-checks the opportunity symbol/report linkage in Python and re-verifies
each position's symbol and trigger id (the trigger -> opportunity -> report
chain is enforced by the scoped join). It projects the stored zone bounds,
trigger, state, and position prices verbatim, and ``_MAX_MARKERS`` caps the
marker list. Both bounds are reported through the payload's
``overlays_truncated`` flag, so a clipped list is never presented as complete.
Switching the timeframe changes only the drawn series: the overlays always come
from the route's report, so a switch can never invent a zone or marker for
another period.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from oft.ports.store import OpportunityRecord, ReportHeader, ReportRecord, StorePort

#: Periods the timeframe switch may offer beyond the report's native period.
SUPPORTED_CHART_PERIODS: tuple[str, ...] = ("15m", "1h", "4h")

#: Bound on the overlay rows considered (each query fetches one extra row to
#: detect truncation).
_OVERLAY_READ_LIMIT = 200
#: Bound on the marker list; a chart never draws an unbounded marker set.
_MAX_MARKERS = 40
#: Bound on one overlay label (untrusted opportunity/position ids).
_MAX_LABEL = 80

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
    except (TypeError, ValueError, RecursionError) as exc:
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


def _payload_object(record_payload_json: str) -> Mapping[str, Any] | None:
    """The persisted payload object, or ``None`` when it is unreadable.

    Opportunity, trigger, and position rows are untrusted persisted JSON: a
    truncated or hand-edited document is skipped, never raised out of a chart
    read.
    """
    try:
        payload = json.loads(record_payload_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _price_text(value: Any) -> str | None:
    """The stored price string when it is a positive finite decimal, else ``None``.

    A missing, zero, negative, non-finite, or unparseable price is omitted. It is
    never coerced to ``"0"``: a marker at zero would be a fabricated price.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except (ArithmeticError, ValueError, InvalidOperation):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return value


def _zone_label(opportunity_id: str, side: str, confidence: Any) -> str:
    """A bounded plain-text label; escaping is the renderer's responsibility."""
    label = f"{opportunity_id} · {side}"
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        label += f" · {confidence:.2f}"
    return label[:_MAX_LABEL]


def _zone_payload(
    record: OpportunityRecord,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One opportunity zone, or ``None`` when the row is not drawable.

    The caller has already checked that the row is linked to the route's report
    (one linkage predicate, not two), so this only validates the zone shape and
    prices. Bounds run through the same positive-finite gate as markers: a
    corrupt bound is skipped rather than advertised as a band the client drops.
    """
    zone = payload.get("zone")
    if not isinstance(zone, Mapping):
        return None
    low = _price_text(zone.get("low"))
    high = _price_text(zone.get("high"))
    if low is None or high is None:
        return None
    side = payload.get("side")
    if side == "LONG":
        trigger_price = high
    elif side == "SHORT":
        trigger_price = low
    else:
        return None
    return {
        "opportunity_id": record.opportunity_id,
        "side": side,
        "state": record.state,
        "low": low,
        "high": high,
        "trigger_price": trigger_price,
        "label": _zone_label(record.opportunity_id, side, payload.get("confidence")),
    }


#: The marker kinds the chart schema accepts; an internal-only alias so a typo
#: cannot silently bypass Pydantic (there is no response_model on the route).
_MarkerKind = Literal["entry", "exit", "stop", "target"]


def _marker(kind: _MarkerKind, price: str, position_id: str | None) -> dict[str, Any]:
    label = f"{kind.upper()} {price}"
    return {
        "kind": kind,
        "price": price,
        "label": label[:_MAX_LABEL],
        "position_id": position_id,
    }


def derive_overlays(
    report: ReportRecord, store: StorePort
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """The report's linked opportunity zones and position markers.

    Bounds, all reported through the third return value:

    * each linked read fetches ``_OVERLAY_READ_LIMIT + 1`` rows so an over-bound
      result is detected, then is sliced back to ``_OVERLAY_READ_LIMIT``;
    * the marker list is capped at ``_MAX_MARKERS``; a further unique marker is
      dropped and counts as truncation.

    The reads are scoped in SQL to the report's symbol and ``source.report_id``
    (positions through trigger -> opportunity -> report), so newer rows for
    other reports can never starve this report. The opportunity symbol/report
    linkage is re-checked in Python; each position's symbol and trigger id are
    re-verified, while its trigger -> opportunity -> report chain is enforced by
    the scoped join. A row whose stored JSON is unreadable is skipped rather
    than failing the chart.
    """
    opportunity_rows = store.list_opportunities(
        symbol=report.symbol,
        report_id=report.report_id,
        limit=_OVERLAY_READ_LIMIT + 1,
    )
    truncated = len(opportunity_rows) > _OVERLAY_READ_LIMIT
    zones: list[dict[str, Any]] = []
    for record in opportunity_rows[:_OVERLAY_READ_LIMIT]:
        payload = _payload_object(record.payload_json)
        if payload is None:
            continue
        if record.symbol != report.symbol or payload.get("symbol") != report.symbol:
            continue
        source = payload.get("source")
        if not isinstance(source, Mapping) or source.get("report_id") != report.report_id:
            continue
        zone = _zone_payload(record, payload)
        if zone is not None:
            zones.append(zone)

    markers: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: _MarkerKind, raw_price: Any, position_id: str | None) -> None:
        nonlocal truncated
        price = _price_text(raw_price)
        if price is None:
            return
        key = (kind, price)
        if key in seen:
            return
        seen.add(key)
        if len(markers) >= _MAX_MARKERS:
            truncated = True
            return
        markers.append(_marker(kind, price, position_id))

    position_rows = store.list_positions(
        symbol=report.symbol,
        report_id=report.report_id,
        limit=_OVERLAY_READ_LIMIT + 1,
    )
    truncated = truncated or len(position_rows) > _OVERLAY_READ_LIMIT
    for position_record in position_rows[:_OVERLAY_READ_LIMIT]:
        payload = _payload_object(position_record.payload_json)
        if payload is None:
            continue
        if position_record.symbol != report.symbol or payload.get("symbol") != report.symbol:
            continue
        trigger_id = payload.get("source_trigger_id")
        if not isinstance(trigger_id, str) or not trigger_id:
            continue
        position_id = position_record.position_id or payload.get("position_id")
        entry = payload.get("avg_entry_price")
        if entry in (None, ""):
            entry = payload.get("entry_price")
        candidates: list[tuple[_MarkerKind, Any]] = [
            ("entry", entry),
            ("stop", payload.get("stop_price")),
        ]
        targets = payload.get("target_prices")
        if isinstance(targets, list):
            candidates.extend(("target", target) for target in targets)
        candidates.append(("exit", payload.get("exit_price")))
        for kind, raw_price in candidates:
            add(kind, raw_price, position_id)

    return zones, markers, truncated


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
        "zones": [],
        "markers": [],
        "overlays_truncated": False,
        "data_quality": "MISSING",
        "quality_reasons": [],
        "reason": reason,
    }


def resolve_chart(
    report: ReportRecord,
    requested_period: str,
    store: StorePort,
    *,
    zones: Sequence[Mapping[str, Any]] = (),
    markers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the chart payload for ``requested_period`` without inventing data.

    ``zones``/``markers`` come from :func:`derive_overlays` for the route's
    report. They are copied verbatim into an ``AVAILABLE`` payload (the route
    overwrites them after resolving); every payload starts with
    ``overlays_truncated`` False, and every ``MISSING`` payload keeps empty
    overlay lists because no series and no overlays are drawn.
    """
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
        "zones": [dict(zone) for zone in zones],
        "markers": [dict(marker) for marker in markers],
        "overlays_truncated": False,
        "data_quality": source.data_quality,
        "quality_reasons": quality_reasons,
        "reason": None,
    }
