"""Unit tests: freshness budgets by data type and injected-clock skew detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oft.application.freshness import (
    AGG_TRADES_STREAM,
    BAR_1D_STREAM,
    BAR_1M_STREAM,
    BAR_4H_STREAM,
    BAR_5M_STREAM,
    BAR_8H_STREAM,
    KLINE_1M_STREAM,
    MARK_STREAM,
    METRICS_STREAM,
    ClockSkewDetector,
    ClockSkewStatus,
    FreshnessBudget,
    FreshnessGuard,
    FreshnessStatus,
    budget_for,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def test_exact_budget_boundary_is_fresh_and_one_microsecond_late_is_stale() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    guard.record(MARK_STREAM, NOW)

    clock.instant = NOW + timedelta(seconds=60)
    stamp = guard.stamp(MARK_STREAM)
    assert stamp is not None
    assert stamp.status is FreshnessStatus.FRESH

    clock.instant = NOW + timedelta(seconds=60, microseconds=1)
    stamp = guard.stamp(MARK_STREAM)
    assert stamp is not None
    assert stamp.status is FreshnessStatus.STALE
    assert stamp.deadline == NOW + timedelta(seconds=60)
    assert stamp.reason == "no update within 60s budget"


def test_bar_4h_budget_is_ten_minutes_after_close() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    close = NOW + timedelta(hours=4)
    guard.record(BAR_4H_STREAM, NOW, closes_at=close)

    clock.instant = close + timedelta(minutes=10)
    assert guard.is_stale(BAR_4H_STREAM) is False
    clock.instant = close + timedelta(minutes=10, microseconds=1)
    assert guard.is_stale(BAR_4H_STREAM) is True


def test_every_data_type_has_a_budget() -> None:
    expected = {
        MARK_STREAM: 60,
        KLINE_1M_STREAM: 120,
        AGG_TRADES_STREAM: 60,
        BAR_1M_STREAM: 120,
        BAR_5M_STREAM: 300,
        BAR_4H_STREAM: 600,
        BAR_8H_STREAM: 600,
        BAR_1D_STREAM: 1800,
        METRICS_STREAM: 900,
    }
    for stream, seconds in expected.items():
        assert budget_for(stream).seconds == seconds


def test_budgets_are_configurable_per_data_type() -> None:
    budgets = {
        MARK_STREAM: FreshnessBudget(MARK_STREAM, 5),
        BAR_1M_STREAM: FreshnessBudget(BAR_1M_STREAM, 30),
    }
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock, budgets=budgets)
    guard.record(MARK_STREAM, NOW)
    guard.record(BAR_1M_STREAM, NOW)

    clock.advance(5)
    assert guard.is_stale(MARK_STREAM) is False
    clock.advance(0.001)
    assert guard.is_stale(MARK_STREAM) is True
    assert guard.is_stale(BAR_1M_STREAM) is False

    assert budget_for(MARK_STREAM, budgets=budgets).seconds == 5
    assert budget_for("unknown_stream").seconds == 60


def test_unknown_stream_is_not_reported_stale() -> None:
    guard = FreshnessGuard(clock=MutableClock(NOW))
    assert guard.stamp("unknown_stream") is None
    assert guard.is_stale("unknown_stream") is False


def test_clock_skew_is_detected_beyond_the_threshold_only() -> None:
    clock = MutableClock(NOW)
    detector = ClockSkewDetector(clock=clock, max_skew_seconds=5)

    assert detector.observe(reference_now=NOW).status is ClockSkewStatus.OK

    clock.advance(5)
    boundary = detector.observe(reference_now=NOW)
    assert boundary.offset_seconds == 5
    assert boundary.detected is False
    assert boundary.status is ClockSkewStatus.OK

    clock.advance(0.001)
    skewed = detector.observe(reference_now=NOW)
    assert skewed.offset_seconds == pytest.approx(5.001)
    assert skewed.detected is True
    assert skewed.status is ClockSkewStatus.SKEWED
    assert skewed.threshold_seconds == 5


def test_clock_skew_uses_the_injected_clock_not_the_wall_clock() -> None:
    reference = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(reference - timedelta(seconds=2))
    detector = ClockSkewDetector(clock=clock, max_skew_seconds=5)

    behind = detector.observe(reference_now=reference)
    assert behind.offset_seconds == -2
    assert behind.detected is False

    clock.instant = reference + timedelta(seconds=6)
    ahead = detector.observe(reference_now=reference)
    assert ahead.offset_seconds == 6
    assert ahead.detected is True


def test_clock_skew_rejects_a_naive_reference() -> None:
    detector = ClockSkewDetector(clock=MutableClock(NOW))
    with pytest.raises(ValueError, match="timezone-aware"):
        detector.observe(reference_now=datetime(2026, 11, 6, 12, 0))


def test_guard_exposes_clock_skew_check_using_its_own_clock() -> None:
    clock = MutableClock(NOW + timedelta(seconds=10))
    guard = FreshnessGuard(clock=clock)

    skew = guard.check_clock_skew(reference_now=NOW)

    assert skew.detected is True
    assert skew.offset_seconds == 10
