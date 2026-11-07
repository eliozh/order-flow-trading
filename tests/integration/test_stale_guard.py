"""Freshness budgets: STALE flips after the budget and clears on fresh data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from oft.application.freshness import (
    DEFAULT_BUDGETS,
    FreshnessGuard,
    FreshnessStatus,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def test_mark_is_stale_after_sixty_seconds_and_clears() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    guard.record("mark", NOW)

    clock.advance(59)
    assert guard.is_stale("mark") is False
    clock.advance(2)
    assert guard.is_stale("mark") is True
    stamp = guard.stamp("mark")
    assert stamp is not None
    assert stamp.status is FreshnessStatus.STALE
    assert stamp.age_seconds is not None and stamp.age_seconds > 60

    guard.record("mark", clock.now())
    assert guard.is_stale("mark") is False
    assert guard.stamp("mark").status is FreshnessStatus.FRESH


def test_kline_is_stale_two_minutes_after_bar_close() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    close = NOW + timedelta(minutes=1)
    guard.record("kline_1m", NOW, closes_at=close)

    clock.instant = close + timedelta(seconds=119)
    assert guard.is_stale("kline_1m") is False
    clock.instant = close + timedelta(seconds=121)
    assert guard.is_stale("kline_1m") is True


def test_metrics_are_stale_fifteen_minutes_after_expected_sample() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    expected = NOW - timedelta(minutes=5)
    guard.record("metrics", NOW - timedelta(minutes=1), expected_at=expected)

    clock.instant = expected + timedelta(minutes=14, seconds=59)
    assert guard.is_stale("metrics") is False
    clock.instant = expected + timedelta(minutes=15, seconds=1)
    assert guard.is_stale("metrics") is True


def test_unknown_stream_is_not_reported_stale() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)

    stamp = guard.stamp("mark")

    assert stamp is None or stamp.status is FreshnessStatus.UNKNOWN
    assert guard.is_stale("mark") is False


def test_budgets_match_the_adr() -> None:
    assert DEFAULT_BUDGETS["mark"].seconds == 60
    assert DEFAULT_BUDGETS["kline_1m"].seconds == 120
    assert DEFAULT_BUDGETS["metrics"].seconds == 900


def test_guard_only_stamps_and_logs_on_transitions() -> None:
    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    guard.record("mark", NOW)
    clock.advance(61)

    with structlog.testing.capture_logs() as logs:
        first = guard.evaluate()
        second = guard.evaluate()

    assert first[0].stale is True
    assert second[0].stale is True
    stale_events = [event for event in logs if event["event"] == "market_data_stale"]
    assert len(stale_events) == 1
    assert stale_events[0]["stream"] == "mark"
    assert "data_quality" in stale_events[0]
    assert stale_events[0]["data_quality"] == "STALE"

    guard.record("mark", clock.now())
    clock.advance(1)
    with structlog.testing.capture_logs() as cleared:
        guard.evaluate()
    assert any(event["event"] == "market_data_fresh" for event in cleared)


def test_guard_exposes_no_trigger_or_risk_path() -> None:
    import inspect

    import oft.application.freshness as freshness

    source = inspect.getsource(freshness)
    assert "oft.domain.trigger" not in source
    assert "oft.domain.risk" not in source
    assert "place_order" not in source
