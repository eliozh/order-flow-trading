"""Frozen trigger snapshots built from the source report's recorded market data.

The production provider reads the opportunity's source report ``snapshot_json``
and converts it with the same mapping the opportunity display path uses. The
seam is deliberate: a later slice can replace the report-backed provider with a
live-metrics one without changing ``TriggerService`` or the evaluator.

No lookahead: a bar whose ``bar_end`` is after the snapshot's own ``as_of`` is
dropped before the context is built. A missing report, a non-object snapshot,
or a missing/timezone-naive ``as_of`` yields ``None`` so the service reports
the opportunity as unavailable instead of raising.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from oft.application.condition_context import evaluation_context_from_snapshot
from oft.domain.opportunities import Opportunity
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot, contract_data_quality
from oft.ports.store import StorePort

_LOGGER = structlog.get_logger(__name__)


class TriggerSnapshotProvider(Protocol):
    """The one seam the trigger service uses to obtain market state."""

    def snapshot_for(self, opportunity: Opportunity) -> TriggerSnapshot | None: ...


def _aware_instant(value: object) -> datetime | None:
    """A strictly timezone-aware instant; a naive value is not a usable ceiling."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def visible_bars(bars: object, ceiling: datetime) -> object:
    """Drop every bar that closes after ``ceiling`` (no lookahead).

    A bar whose ``bar_end`` is missing or malformed cannot be proven to be at
    or before the ceiling and is excluded. Fail closed: a non-list ``bars``
    value or any non-object element yields an empty list. Previously a
    malformed element returned the whole unfiltered list, which could leak
    future bars to any consumer that trusts this helper alone.
    """
    if not isinstance(bars, list):
        return []
    visible: list[object] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            _LOGGER.warning("trigger_bar_dropped", reason="bar is not an object")
            return []
        end = _aware_instant(bar.get("bar_end"))
        if end is None:
            continue
        if end <= ceiling:
            visible.append(bar)
    return visible


def _quality_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


class ReportSnapshotProvider:
    """Build a frozen snapshot from the opportunity's source report."""

    def __init__(self, *, store: StorePort) -> None:
        self._store = store

    def snapshot_for(self, opportunity: Opportunity) -> TriggerSnapshot | None:
        report_id = str(opportunity.source.get("report_id", ""))
        if not report_id:
            return self._unavailable(opportunity, "the source report id is missing")
        report = self._store.get_report(report_id)
        if report is None:
            return self._unavailable(opportunity, "the source report is missing")
        try:
            raw = json.loads(report.snapshot_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return self._unavailable(opportunity, "the report snapshot is malformed")
        if not isinstance(raw, Mapping):
            return self._unavailable(opportunity, "the report snapshot is not an object")
        snapshot: dict[str, Any] = dict(raw)
        ceiling = _aware_instant(snapshot.get("as_of"))
        if ceiling is None:
            return self._unavailable(opportunity, "the report snapshot has no timezone-aware as_of")
        as_of = utc_timestamp(ceiling)
        filtered = {**snapshot, "bars": visible_bars(snapshot.get("bars"), ceiling)}
        context = evaluation_context_from_snapshot(filtered, as_of=as_of)
        snapshot_id = snapshot.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            # Empty: the evaluator derives one id from the snapshot identity
            # hash, the single scheme shared by the provider, manual, and
            # legacy paths.
            snapshot_id = ""
        return TriggerSnapshot(
            symbol=opportunity.symbol,
            as_of=as_of,
            last_price=context.last_price,
            previous_price=context.previous_price,
            tick_size=context.tick_size,
            bar_timestamps=context.bar_timestamps,
            price_series=context.price_series,
            delta_series=context.delta_series,
            cvd_series=context.cvd_series,
            volume_series=context.volume_series,
            oi_series=context.oi_series,
            delta=context.delta,
            cvd=context.cvd,
            oi_delta_pct=context.oi_delta_pct,
            funding_rate=context.funding_rate,
            liquidation_notional=context.liquidation_notional,
            volume=context.volume,
            footprint_imbalance=context.footprint_imbalance,
            session_state=context.session_state,
            data_quality=contract_data_quality(snapshot.get("data_quality")),
            quality_reasons=_quality_reasons(snapshot.get("quality_reasons")),
            snapshot_id=snapshot_id,
        )

    def _unavailable(self, opportunity: Opportunity, reason: str) -> TriggerSnapshot | None:
        _LOGGER.warning(
            "trigger_snapshot_unavailable",
            opportunity_id=opportunity.opportunity_id,
            symbol=opportunity.symbol,
            reason=reason,
        )
        return None


__all__ = [
    "ReportSnapshotProvider",
    "TriggerSnapshotProvider",
    "visible_bars",
]
