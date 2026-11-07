"""Unit tests for open-interest delta and irregular TradFi resampling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.application.oi_funding_service import OiFundingService, load_metrics_fixture
from oft.domain.data_quality import DataQuality
from oft.domain.metrics.open_interest import (
    OpenInterestSample,
    compute_open_interest,
)

WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(hours=4)
MARKET = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "market"


def _sample(
    minute: int,
    open_interest: str,
    value: str = "1000000.00",
    *,
    long_short_ratio: str | None = "1.10",
    taker_ratio: str | None = "0.52",
) -> OpenInterestSample:
    return OpenInterestSample(
        ts=WINDOW_START + timedelta(minutes=minute),
        open_interest=Decimal(open_interest),
        open_interest_value=Decimal(value),
        long_short_ratio=None if long_short_ratio is None else Decimal(long_short_ratio),
        taker_ratio=None if taker_ratio is None else Decimal(taker_ratio),
    )


def _compute(samples: tuple[OpenInterestSample, ...], **kwargs: object) -> object:
    return compute_open_interest(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        **kwargs,  # type: ignore[arg-type]
    )


def test_regular_five_minute_sampling_is_measured_and_keeps_the_delta() -> None:
    samples = (_sample(0, "100.0"), _sample(5, "105.0"), _sample(10, "102.0"))

    result = _compute(samples)

    assert result.sampling == "REGULAR"
    assert result.quality.data_quality == DataQuality.PARTIAL
    assert "span" in " ".join(result.quality.quality_reasons)
    assert result.delta_over_window == Decimal("2.0")
    assert len(result.points) == 3
    assert [point.ts for point in result.points] == [
        WINDOW_START,
        WINDOW_START + timedelta(minutes=5),
        WINDOW_START + timedelta(minutes=10),
    ]
    assert all(point.estimated is False for point in result.points)
    assert result.last is not None
    assert result.last.open_interest == Decimal("102.0")
    assert result.unit == "BASE"


def test_irregular_tradfi_timestamps_are_resampled_and_stamped_estimated() -> None:
    samples = (_sample(0, "100.0"), _sample(7, "105.0"), _sample(12, "103.0"))

    result = _compute(samples)

    assert result.sampling == "IRREGULAR_RESAMPLED"
    assert result.quality.data_quality == DataQuality.ESTIMATED
    reasons = " ".join(result.quality.quality_reasons)
    assert "resampled" in reasons.lower()
    assert "ESTIMATED" in reasons
    assert "not measured" in reasons
    assert result.delta_over_window == Decimal("3.0")
    assert [point.ts for point in result.points] == [
        WINDOW_START,
        WINDOW_START + timedelta(minutes=5),
        WINDOW_START + timedelta(minutes=7),
        WINDOW_START + timedelta(minutes=10),
        WINDOW_START + timedelta(minutes=12),
    ]
    assert [point.open_interest for point in result.points] == [
        Decimal("100.0"),
        Decimal("100.0"),
        Decimal("105.0"),
        Decimal("105.0"),
        Decimal("103.0"),
    ]
    original = {
        WINDOW_START,
        WINDOW_START + timedelta(minutes=7),
        WINDOW_START + timedelta(minutes=12),
    }
    assert all(point.estimated for point in result.points if point.ts not in original)
    assert all(not point.estimated for point in result.points if point.ts in original)


def test_resampled_ratio_and_value_are_carried_forward_but_marked_estimated() -> None:
    samples = (
        _sample(0, "100.0", "1000000.00", long_short_ratio=None, taker_ratio="0.52"),
        _sample(12, "110.0", "1100000.00", long_short_ratio="1.30", taker_ratio=None),
    )

    result = _compute(samples)

    estimate = next(
        point for point in result.points if point.ts == WINDOW_START + timedelta(minutes=5)
    )
    assert estimate.estimated is True
    assert estimate.open_interest == Decimal("100.0")
    assert estimate.open_interest_value == Decimal("1000000.00")
    assert estimate.long_short_ratio is None
    assert estimate.taker_ratio == Decimal("0.52")


def test_regular_sampling_accepts_a_single_measured_point() -> None:
    result = _compute((_sample(0, "100.0"),))

    assert result.sampling == "REGULAR"
    assert result.quality.data_quality == DataQuality.PARTIAL
    assert "span" in " ".join(result.quality.quality_reasons)
    assert result.delta_over_window == Decimal("0")
    assert result.last is not None
    assert result.last.open_interest == Decimal("100.0")


def test_gapped_regular_grid_is_resampled_never_measured() -> None:
    result = _compute((_sample(0, "100.0"), _sample(10, "110.0")))

    assert result.sampling == "IRREGULAR_RESAMPLED"
    assert result.quality.data_quality == DataQuality.ESTIMATED
    assert [point.ts for point in result.points] == [
        WINDOW_START,
        WINDOW_START + timedelta(minutes=5),
        WINDOW_START + timedelta(minutes=10),
    ]
    assert result.points[1].estimated is True


def test_empty_or_out_of_window_samples_are_missing_not_zero_measured() -> None:
    result = _compute((_sample(240, "100.0"),))

    assert result.points == ()
    assert result.last is None
    assert result.delta_over_window == Decimal("0")
    assert result.quality.data_quality == DataQuality.MISSING
    assert any("no open-interest samples" in reason for reason in result.quality.quality_reasons)


def test_validation_rejects_bad_samples_and_parameters() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _compute((_sample(0, "100.0"), _sample(0, "101.0")))

    naive = OpenInterestSample(
        ts=datetime(2026, 10, 1, 0, 0),
        open_interest=Decimal("1"),
        open_interest_value=Decimal("1"),
    )
    with pytest.raises(ValueError, match="timezone"):
        _compute((naive,))

    with pytest.raises(ValueError, match="unit"):
        _compute((_sample(0, "100.0"),), unit="USD")

    with pytest.raises(ValueError):
        _compute((_sample(0, "100.0"),), target_interval=timedelta(0))


def test_oi_funding_service_reads_the_irregular_xau_fixture() -> None:
    service = OiFundingService.from_csv(MARKET / "xauusdt_metrics_sample.csv")

    oi = service.open_interest(window_start=WINDOW_START, window_end=WINDOW_END)
    funding = service.funding(as_of=WINDOW_START + timedelta(minutes=30))

    assert oi.sampling == "IRREGULAR_RESAMPLED"
    assert oi.quality.data_quality == DataQuality.ESTIMATED
    assert oi.delta_over_window == Decimal("15.0")
    assert funding.current_rate == Decimal("0.00010000")
    assert funding.next_funding_time == WINDOW_START + timedelta(hours=8)
    assert funding.interval_hours == 8


def test_oi_funding_service_rejects_missing_and_malformed_fixtures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        OiFundingService.from_csv(tmp_path / "missing.csv")

    malformed = tmp_path / "malformed.csv"
    malformed.write_text("symbol,record_type\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_metrics_fixture(malformed)


def test_regular_sampling_complete_only_when_samples_span_the_window() -> None:
    samples = tuple(_sample(minute, "100.0") for minute in range(0, 4 * 60, 5))

    result = _compute(samples)

    assert result.sampling == "REGULAR"
    assert result.quality.data_quality == DataQuality.COMPLETE
    assert result.quality.quality_reasons == ()


def test_negative_optional_ratio_is_rejected() -> None:
    with pytest.raises(ValueError, match="long_short_ratio"):
        _compute((_sample(0, "100.0", long_short_ratio="-1"),))


def test_open_interest_validation_and_contract_cover_all_rejections() -> None:
    from decimal import Decimal

    from oft.domain.metrics.open_interest import OpenInterestResult
    from oft.domain.metrics.types import DataQuality, MetricQuality

    for field, value in (
        ("open_interest", Decimal("-1")),
        ("open_interest_value", Decimal("-1")),
    ):
        kwargs = {
            "ts": WINDOW_START,
            "open_interest": Decimal("1"),
            "open_interest_value": Decimal("1"),
        }
        kwargs[field] = value
        with pytest.raises(ValueError):
            _compute((OpenInterestSample(**kwargs),))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timezone-aware"):
        compute_open_interest(
            (_sample(0, "100.0"),),
            window_start=datetime(2026, 10, 1),
            window_end=WINDOW_END,
        )
    with pytest.raises(ValueError, match="after window_start"):
        compute_open_interest(
            (_sample(0, "100.0"),),
            window_start=WINDOW_START,
            window_end=WINDOW_START,
        )

    empty = OpenInterestResult(
        unit="BASE",
        sampling="REGULAR",
        points=(),
        delta_over_window=Decimal("0"),
        last=None,
        quality=MetricQuality(DataQuality.MISSING, ("missing",)),
    )
    with pytest.raises(ValueError, match="no measured point"):
        empty.to_contract()


def test_irregular_samples_that_do_not_span_the_window_are_estimated_and_reasoned() -> None:
    samples = (
        _sample(0, "100.0"),
        _sample(3, "101.0"),
        _sample(9, "102.0"),
    )
    result = _compute(samples)
    assert result.sampling == "IRREGULAR_RESAMPLED"
    assert any("do not span" in reason for reason in result.quality.quality_reasons)


def test_irregular_samples_that_span_the_window_skip_the_missing_span_reason() -> None:
    samples = (
        _sample(0, "100.0"),
        _sample(3 * 60 + 59, "101.0"),
    )
    result = _compute(samples)
    assert result.sampling == "IRREGULAR_RESAMPLED"
    assert not any("do not span" in reason for reason in result.quality.quality_reasons)
