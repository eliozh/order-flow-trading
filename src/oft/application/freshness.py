"""Freshness budgets: stamp and log STALE, never touch a trigger/risk path.

Budgets follow ADR-0008: live mark 60 s, a 1 m bar 2 min after close, and
metrics 15 min after the expected sample. This module has no side effects other
than updating in-memory stamps and structured log events; it cannot place an
order and does not import the risk or trigger domains.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import structlog

from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

MARK_STREAM = "mark"
KLINE_1M_STREAM = "kline_1m"
METRICS_STREAM = "metrics"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FreshnessBudget:
    """How long a stream may remain silent before it is stale."""

    stream: str
    seconds: float


DEFAULT_BUDGETS: Mapping[str, FreshnessBudget] = {
    MARK_STREAM: FreshnessBudget(MARK_STREAM, 60.0),
    KLINE_1M_STREAM: FreshnessBudget(KLINE_1M_STREAM, 120.0),
    METRICS_STREAM: FreshnessBudget(METRICS_STREAM, 900.0),
}


@dataclass(frozen=True)
class FreshnessStamp:
    """The freshness verdict for one stream at one instant."""

    stream: str
    status: FreshnessStatus
    age_seconds: float | None
    budget_seconds: float
    observed_at: datetime | None
    deadline: datetime | None
    reason: str | None = None

    @property
    def stale(self) -> bool:
        return self.status is FreshnessStatus.STALE

    @property
    def data_quality(self) -> str:
        return "STALE" if self.stale else "COMPLETE"


@dataclass
class _Entry:
    observed_at: datetime
    deadline: datetime
    logged_stale: bool = False
    pending_fresh_log: bool = False


class FreshnessGuard:
    """Track per-stream freshness and log only on STALE/FRESH transitions."""

    def __init__(
        self,
        *,
        clock: ClockPort,
        budgets: Mapping[str, FreshnessBudget] | None = None,
    ) -> None:
        self._clock = clock
        merged = dict(DEFAULT_BUDGETS)
        if budgets is not None:
            merged.update(budgets)
        self._budgets = merged
        self._entries: dict[str, _Entry] = {}
        self._logger = structlog.get_logger(__name__)

    def _budget(self, stream: str) -> FreshnessBudget:
        return self._budgets.get(stream, FreshnessBudget(stream, 60.0))

    def record(
        self,
        stream: str,
        observed_at: datetime,
        *,
        closes_at: datetime | None = None,
        expected_at: datetime | None = None,
    ) -> FreshnessStamp:
        """Record fresh data; the deadline is based on close/expected time."""
        budget = self._budget(stream)
        anchor = closes_at or expected_at or observed_at
        previous = self._entries.get(stream)
        entry = _Entry(
            observed_at=observed_at,
            deadline=anchor + timedelta(seconds=budget.seconds),
            pending_fresh_log=bool(previous is not None and previous.logged_stale),
        )
        self._entries[stream] = entry
        return self._stamp(stream, entry, self._clock.now())

    def stamp(self, stream: str, now: datetime | None = None) -> FreshnessStamp | None:
        entry = self._entries.get(stream)
        if entry is None:
            return None
        return self._stamp(stream, entry, now or self._clock.now())

    def evaluate(self, now: datetime | None = None) -> tuple[FreshnessStamp, ...]:
        """Recompute every stamp and log transitions; safe to call repeatedly."""
        instant = now or self._clock.now()
        stamps: list[FreshnessStamp] = []
        for stream, entry in self._entries.items():
            stamp = self._stamp(stream, entry, instant)
            if stamp.stale and not entry.logged_stale:
                entry.logged_stale = True
                self._logger.warning(
                    "market_data_stale",
                    stream=stream,
                    data_quality=stamp.data_quality,
                    age_seconds=stamp.age_seconds,
                    budget_seconds=stamp.budget_seconds,
                    reason=stamp.reason,
                )
            elif not stamp.stale and entry.logged_stale:
                entry.logged_stale = False
                entry.pending_fresh_log = False
                self._logger.info(
                    "market_data_fresh",
                    stream=stream,
                    data_quality=stamp.data_quality,
                    age_seconds=stamp.age_seconds,
                )
            elif not stamp.stale and entry.pending_fresh_log:
                entry.pending_fresh_log = False
                self._logger.info(
                    "market_data_fresh",
                    stream=stream,
                    data_quality=stamp.data_quality,
                    age_seconds=stamp.age_seconds,
                )
            stamps.append(stamp)
        return tuple(stamps)

    def is_stale(self, stream: str, now: datetime | None = None) -> bool:
        stamp = self.stamp(stream, now)
        return bool(stamp is not None and stamp.stale)

    def clear(self, stream: str) -> None:
        self._entries.pop(stream, None)

    def snapshot(self, now: datetime | None = None) -> dict[str, dict[str, object]]:
        """Machine-readable state for ``oft status`` / the WS state file."""
        instant = now or self._clock.now()
        snapshot: dict[str, dict[str, object]] = {}
        for stream, entry in self._entries.items():
            stamp = self._stamp(stream, entry, instant)
            snapshot[stream] = {
                "status": stamp.status.value,
                "data_quality": stamp.data_quality,
                "stale": stamp.stale,
                "age_seconds": stamp.age_seconds,
                "budget_seconds": stamp.budget_seconds,
                "last_event_time": (
                    utc_timestamp(entry.observed_at) if entry.observed_at else None
                ),
                "deadline": utc_timestamp(entry.deadline) if entry.deadline else None,
                "reason": stamp.reason,
            }
        return snapshot

    def _stamp(self, stream: str, entry: _Entry, now: datetime) -> FreshnessStamp:
        budget = self._budget(stream)
        age = (now - entry.observed_at).total_seconds()
        stale = now > entry.deadline
        return FreshnessStamp(
            stream=stream,
            status=FreshnessStatus.STALE if stale else FreshnessStatus.FRESH,
            age_seconds=age,
            budget_seconds=budget.seconds,
            observed_at=entry.observed_at,
            deadline=entry.deadline,
            reason=(f"no update within {budget.seconds:g}s budget" if stale else None),
        )


__all__ = [
    "DEFAULT_BUDGETS",
    "KLINE_1M_STREAM",
    "MARK_STREAM",
    "METRICS_STREAM",
    "FreshnessBudget",
    "FreshnessGuard",
    "FreshnessStamp",
    "FreshnessStatus",
]
