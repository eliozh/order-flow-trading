"""Unit tests for funding rates, intervals, next time, and missing periods."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from oft.domain.data_quality import DataQuality
from oft.domain.metrics.funding import FundingSample, compute_funding

BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _sample(
    hour: int,
    rate: str,
    *,
    interval: int = 8,
    mark: str = "70000.00",
    index: str = "70001.00",
) -> FundingSample:
    return FundingSample(
        ts=BASE + timedelta(hours=hour),
        rate=Decimal(rate),
        mark_price=Decimal(mark),
        index_price=Decimal(index),
        interval_hours=interval,
    )


def test_eight_hour_funding_summary_and_history() -> None:
    samples = (_sample(0, "0.00010000"), _sample(8, "0.00020000", mark="70100.00"))

    result = compute_funding(samples, as_of=BASE + timedelta(hours=10))

    assert result.quality.data_quality == DataQuality.COMPLETE
    assert result.current_rate == Decimal("0.00020000")
    assert result.interval_hours == 8
    assert result.next_funding_time == BASE + timedelta(hours=16)
    assert result.mark_price == Decimal("70100.00")
    assert result.index_price == Decimal("70001.00")
    assert [point.rate for point in result.history] == [
        Decimal("0.00010000"),
        Decimal("0.00020000"),
    ]
    assert result.missing_periods == ()


def test_four_hour_interval_is_supported() -> None:
    samples = (_sample(0, "0.00010", interval=4), _sample(4, "0.00020", interval=4))

    result = compute_funding(samples, as_of=BASE + timedelta(hours=5))

    assert result.interval_hours == 4
    assert result.next_funding_time == BASE + timedelta(hours=8)
    assert result.quality.data_quality == DataQuality.COMPLETE


def test_next_funding_time_rolls_forward_past_the_as_of_instant() -> None:
    samples = (_sample(0, "0.0001"), _sample(8, "0.0002"))

    at_boundary = compute_funding(samples, as_of=BASE + timedelta(hours=16))
    later = compute_funding(samples, as_of=BASE + timedelta(hours=17))

    assert at_boundary.next_funding_time == BASE + timedelta(hours=24)
    assert later.next_funding_time == BASE + timedelta(hours=24)


def test_missing_period_is_reported_missing_and_never_zero_filled() -> None:
    samples = (_sample(0, "0.0001"), _sample(24, "0.0002"))

    result = compute_funding(samples, as_of=BASE + timedelta(hours=25))

    assert result.quality.data_quality == DataQuality.MISSING
    assert result.missing_periods == (BASE + timedelta(hours=8), BASE + timedelta(hours=16))
    reasons = " ".join(result.quality.quality_reasons)
    assert "2026-10-01T08:00:00.000Z" in reasons
    assert "2026-10-01T16:00:00.000Z" in reasons
    assert "not treated as zero" in reasons
    assert all(point.rate != Decimal("0") for point in result.history)
    assert len(result.history) == 2


def test_no_funding_data_is_missing_not_a_zero_rate() -> None:
    result = compute_funding((), as_of=BASE)

    assert result.quality.data_quality == DataQuality.MISSING
    assert result.current_rate is None
    assert result.next_funding_time is None
    assert result.interval_hours is None
    assert result.mark_price is None
    assert result.index_price is None
    assert result.history == ()
    assert result.missing_periods == ()


def test_future_samples_are_ignored_for_the_current_period() -> None:
    samples = (_sample(0, "0.0001"), _sample(8, "0.0002"), _sample(24, "0.0003"))

    result = compute_funding(samples, as_of=BASE + timedelta(hours=9))

    assert result.current_rate == Decimal("0.0002")
    assert result.next_funding_time == BASE + timedelta(hours=16)
    assert len(result.history) == 2


def test_validation_rejects_bad_intervals_and_timestamps() -> None:
    with pytest.raises(ValueError, match="interval"):
        compute_funding((_sample(0, "0.0001", interval=5),), as_of=BASE)

    mixed = (_sample(0, "0.0001", interval=4), _sample(4, "0.0002", interval=8))
    with pytest.raises(ValueError, match="interval"):
        compute_funding(mixed, as_of=BASE + timedelta(hours=5))

    naive = datetime(2026, 10, 1, 0, 0)
    with pytest.raises(ValueError, match="timezone"):
        FundingSample(
            ts=naive,
            rate=Decimal("0.0001"),
            mark_price=Decimal("1"),
            index_price=Decimal("1"),
            interval_hours=8,
        )

    with pytest.raises(ValueError, match="timezone"):
        compute_funding((_sample(0, "0.0001"),), as_of=datetime(2026, 10, 1, 1, 0))

    with pytest.raises(ValueError, match="duplicate"):
        compute_funding((_sample(0, "0.0001"), _sample(0, "0.0002")), as_of=BASE)


def test_trailing_missing_funding_period_is_not_complete() -> None:
    samples = (_sample(0, "0.0001"), _sample(8, "0.0002"))

    result = compute_funding(samples, as_of=BASE + timedelta(hours=16))

    assert result.quality.data_quality != DataQuality.COMPLETE
    assert result.missing_periods == (BASE + timedelta(hours=16),)
    reasons = " ".join(result.quality.quality_reasons)
    assert "2026-10-01T16:00:00.000Z" in reasons
    assert "not treated as zero" in reasons
    assert result.next_funding_time == BASE + timedelta(hours=24)
