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
from typing import TYPE_CHECKING

import structlog

from oft.application.ws_state import WS_STATE_FILENAME, read_ws_state
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

if TYPE_CHECKING:
    from oft.config import Settings

MARK_STREAM = "mark"
KLINE_1M_STREAM = "kline_1m"
METRICS_STREAM = "metrics"
AGG_TRADES_STREAM = "agg_trades"
BAR_1M_STREAM = "bar_1m"
BAR_5M_STREAM = "bar_5m"
BAR_15M_STREAM = "bar_15m"
BAR_1H_STREAM = "bar_1h"
BAR_4H_STREAM = "bar_4h"
BAR_8H_STREAM = "bar_8h"
BAR_1D_STREAM = "bar_1d"

DEFAULT_MAX_CLOCK_SKEW_SECONDS = 5.0


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
    AGG_TRADES_STREAM: FreshnessBudget(AGG_TRADES_STREAM, 60.0),
    BAR_1M_STREAM: FreshnessBudget(BAR_1M_STREAM, 120.0),
    BAR_5M_STREAM: FreshnessBudget(BAR_5M_STREAM, 300.0),
    BAR_15M_STREAM: FreshnessBudget(BAR_15M_STREAM, 600.0),
    BAR_1H_STREAM: FreshnessBudget(BAR_1H_STREAM, 600.0),
    BAR_4H_STREAM: FreshnessBudget(BAR_4H_STREAM, 600.0),
    BAR_8H_STREAM: FreshnessBudget(BAR_8H_STREAM, 600.0),
    BAR_1D_STREAM: FreshnessBudget(BAR_1D_STREAM, 1800.0),
    METRICS_STREAM: FreshnessBudget(METRICS_STREAM, 900.0),
}


def budget_for(
    stream: str, *, budgets: Mapping[str, FreshnessBudget] | None = None
) -> FreshnessBudget:
    """Resolve one data type's budget, merging configurable overrides."""
    merged = dict(DEFAULT_BUDGETS)
    if budgets is not None:
        merged.update(budgets)
    return merged.get(stream, FreshnessBudget(stream, 60.0))


class ClockSkewStatus(StrEnum):
    OK = "OK"
    SKEWED = "SKEWED"


@dataclass(frozen=True)
class ClockSkew:
    """Local clock offset from an injected reference instant."""

    offset_seconds: float
    threshold_seconds: float
    status: ClockSkewStatus

    @property
    def detected(self) -> bool:
        return self.status is ClockSkewStatus.SKEWED


class ClockSkewDetector:
    """Compare an injected local clock with a trusted reference instant.

    The reference is normally an exchange server/event timestamp. The local
    clock is always the injected :class:`ClockPort`, never the wall clock, so
    replay and tests are deterministic.
    """

    def __init__(self, *, clock: ClockPort, max_skew_seconds: float = 5.0) -> None:
        if max_skew_seconds < 0:
            raise ValueError("max_skew_seconds must be non-negative")
        self._clock = clock
        self._threshold = max_skew_seconds

    def observe(self, *, reference_now: datetime) -> ClockSkew:
        if reference_now.tzinfo is None:
            raise ValueError("reference_now must be timezone-aware")
        offset = (self._clock.now() - reference_now).total_seconds()
        status = ClockSkewStatus.SKEWED if abs(offset) > self._threshold else ClockSkewStatus.OK
        return ClockSkew(
            offset_seconds=offset,
            threshold_seconds=self._threshold,
            status=status,
        )


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

    def check_clock_skew(
        self,
        *,
        reference_now: datetime,
        max_skew_seconds: float = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    ) -> ClockSkew:
        """Detect local-clock drift against a trusted external instant."""
        return ClockSkewDetector(clock=self._clock, max_skew_seconds=max_skew_seconds).observe(
            reference_now=reference_now
        )

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


LIVE_SCAN_STREAMS: tuple[str, ...] = (KLINE_1M_STREAM, MARK_STREAM)
_LIVE_CONNECTION_STATES = frozenset({"SUBSCRIBED"})


@dataclass(frozen=True)
class LiveSourceVerdict:
    """Whether persisted live input may drive an LLM report right now."""

    fresh: bool
    reason: str | None
    streams: tuple[str, ...]
    status: FreshnessStatus
    checked_at: datetime


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def evaluate_live_source(
    freshness: Mapping[str, Mapping[str, object]] | None,
    *,
    now: datetime,
    connection_state: str | None = None,
    streams: tuple[str, ...] = LIVE_SCAN_STREAMS,
) -> LiveSourceVerdict:
    """Evaluate the persisted live freshness stamps against a strict budget.

    Missing stamps, a disconnected feed, and expired deadlines all fail closed:
    stale live input may still produce a CALC report, but it can never drive a
    narrative or a trigger. The returned reason is always explicit.
    """
    if not freshness:
        return LiveSourceVerdict(
            fresh=False,
            reason="live ingestion state is unavailable; the LLM stage is skipped",
            streams=streams,
            status=FreshnessStatus.UNKNOWN,
            checked_at=now,
        )
    if connection_state is None:
        return LiveSourceVerdict(
            fresh=False,
            reason="live ingestion connection state is unavailable; the LLM stage is skipped",
            streams=streams,
            status=FreshnessStatus.UNKNOWN,
            checked_at=now,
        )
    if connection_state not in _LIVE_CONNECTION_STATES:
        return LiveSourceVerdict(
            fresh=False,
            reason=f"live feed is {connection_state}; the LLM stage is skipped",
            streams=streams,
            status=FreshnessStatus.STALE,
            checked_at=now,
        )
    for stream in streams:
        record = freshness.get(stream)
        if not isinstance(record, Mapping):
            return LiveSourceVerdict(
                fresh=False,
                reason=f"{stream} freshness is missing; the LLM stage is skipped",
                streams=streams,
                status=FreshnessStatus.UNKNOWN,
                checked_at=now,
            )
        deadline = _parse_instant(record.get("deadline"))
        if deadline is None:
            return LiveSourceVerdict(
                fresh=False,
                reason=f"{stream} freshness deadline is unreadable; the LLM stage is skipped",
                streams=streams,
                status=FreshnessStatus.UNKNOWN,
                checked_at=now,
            )
        if now > deadline:
            budget = record.get("budget_seconds")
            budget_text = f"{budget:g}s" if isinstance(budget, (int, float)) else "its"
            return LiveSourceVerdict(
                fresh=False,
                reason=(
                    f"{stream} is stale: no update within {budget_text} budget "
                    f"(deadline {utc_timestamp(deadline)}); the LLM stage is skipped"
                ),
                streams=streams,
                status=FreshnessStatus.STALE,
                checked_at=now,
            )
    return LiveSourceVerdict(
        fresh=True,
        reason=None,
        streams=streams,
        status=FreshnessStatus.FRESH,
        checked_at=now,
    )


def read_live_source_state(
    settings: Settings,
) -> tuple[str | None, Mapping[str, Mapping[str, object]] | None]:
    """Read the shared ``ws_state.json`` written by live ingestion.

    Delegates to the shared WS-state reader so the daemon and the scan path
    can never drift on the file name, location, or shape.
    """
    state = read_ws_state(settings)
    if state is None:
        return None, None
    connection_state = state.connection_state or None
    freshness = state.freshness or None
    return connection_state, freshness


def check_live_source(settings: Settings, clock: ClockPort) -> LiveSourceVerdict:
    """Combine the persisted live state with the injected clock."""
    connection_state, freshness = read_live_source_state(settings)
    return evaluate_live_source(
        freshness,
        now=clock.now(),
        connection_state=connection_state,
    )


__all__ = [
    "AGG_TRADES_STREAM",
    "BAR_15M_STREAM",
    "BAR_1D_STREAM",
    "BAR_1H_STREAM",
    "BAR_1M_STREAM",
    "BAR_4H_STREAM",
    "BAR_5M_STREAM",
    "BAR_8H_STREAM",
    "DEFAULT_BUDGETS",
    "DEFAULT_MAX_CLOCK_SKEW_SECONDS",
    "KLINE_1M_STREAM",
    "LIVE_SCAN_STREAMS",
    "MARK_STREAM",
    "METRICS_STREAM",
    "WS_STATE_FILENAME",
    "ClockSkew",
    "ClockSkewDetector",
    "ClockSkewStatus",
    "FreshnessBudget",
    "FreshnessGuard",
    "FreshnessStamp",
    "FreshnessStatus",
    "LiveSourceVerdict",
    "budget_for",
    "check_live_source",
    "evaluate_live_source",
    "read_live_source_state",
]
