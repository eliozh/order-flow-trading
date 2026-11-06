"""Unit tests for deterministic CVD/delta metrics and the kline cross-check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from oft.domain.events import AggTrade
from oft.domain.metrics.cvd import (
    assert_kline_cross_check,
    compute_cvd,
    kline_delta,
    taker_buy_ratio,
)
from oft.domain.metrics.types import MetricWindow

WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


def _trade(
    trade_id: int,
    quantity: str,
    price: str,
    *,
    is_buyer_maker: bool,
    day: int = 1,
    hour: int = 0,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=trade_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=trade_id * 10,
        last_trade_id=trade_id * 10,
        transact_time=datetime(2026, 10, day, hour, 0, tzinfo=UTC),
        is_buyer_maker=is_buyer_maker,
    )


def _fixture_week() -> tuple[AggTrade, ...]:
    """The 21 unique rows of tests/fixtures/archive/*2026-10-01..07*."""
    trades: list[AggTrade] = []
    for day in range(1, 8):
        base = (day - 1) * 3
        trades.extend(
            (
                _trade(base + 1, "0.10", "70000.00", is_buyer_maker=False, day=day),
                _trade(base + 2, "0.20", "70001.00", is_buyer_maker=True, day=day),
                _trade(base + 3, "0.30", "70002.00", is_buyer_maker=False, day=day),
            )
        )
    return tuple(trades)


def test_buy_sell_mapping_and_one_bar_summary() -> None:
    trades = (
        _trade(1, "2.00", "70000.00", is_buyer_maker=False),
        _trade(2, "0.50", "70001.00", is_buyer_maker=True),
        _trade(3, "2.25", "70002.00", is_buyer_maker=False),
        _trade(4, "2.25", "70003.00", is_buyer_maker=True),
    )

    result = compute_cvd(
        trades,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=4),
    )

    assert result.unit == "BASE"
    assert result.window == MetricWindow(WINDOW_START, WINDOW_START + timedelta(hours=4))
    assert len(result.points) == 1
    point = result.points[0]
    assert point.buy_volume == Decimal("4.25")
    assert point.sell_volume == Decimal("2.75")
    assert point.delta == Decimal("1.50")
    assert point.cvd == Decimal("1.50")
    assert point.taker_buy_ratio == Decimal("0.60714286")
    assert result.summary.delta == Decimal("1.50")
    assert result.summary.cvd_change == Decimal("1.50")
    assert result.summary.buy_volume == Decimal("4.25")
    assert result.summary.sell_volume == Decimal("2.75")
    assert result.divergence_flags == ()


def test_cvd_accumulates_across_bars() -> None:
    trades = (
        _trade(1, "2.00", "70000.00", is_buyer_maker=False),
        _trade(2, "0.50", "70001.00", is_buyer_maker=True),
        _trade(3, "0.75", "70002.00", is_buyer_maker=False, hour=4),
        _trade(4, "1.00", "70003.00", is_buyer_maker=True, hour=4),
    )

    result = compute_cvd(
        trades,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=8),
    )

    assert len(result.points) == 2
    assert result.points[0].delta == Decimal("1.50")
    assert result.points[0].cvd == Decimal("1.50")
    assert result.points[1].delta == Decimal("-0.25")
    assert result.points[1].cvd == Decimal("1.25")
    assert result.summary.delta == Decimal("1.25")
    assert result.summary.cvd_change == Decimal("1.25")


def test_empty_window_returns_zero_summary_and_no_points() -> None:
    result = compute_cvd(
        (),
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result.points == ()
    assert result.summary.delta == Decimal("0")
    assert result.summary.cvd_change == Decimal("0")
    assert result.summary.buy_volume == Decimal("0")
    assert result.summary.sell_volume == Decimal("0")


def test_taker_buy_ratio_is_none_for_zero_volume() -> None:
    assert taker_buy_ratio(Decimal("0"), Decimal("0")) is None
    assert taker_buy_ratio(Decimal("3"), Decimal("1")) == Decimal("0.75000000")


def test_week_fixture_produces_seven_exact_bars() -> None:
    result = compute_cvd(
        _fixture_week(),
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert len(result.points) == 7
    for index, point in enumerate(result.points):
        assert point.bar_start == WINDOW_START + timedelta(days=index)
        assert point.bar_end == WINDOW_START + timedelta(days=index, hours=4)
        assert point.buy_volume == Decimal("0.40")
        assert point.sell_volume == Decimal("0.20")
        assert point.delta == Decimal("0.20")
        assert point.cvd == Decimal("0.20") * (index + 1)
        assert point.taker_buy_ratio == Decimal("0.66666667")
    assert result.summary.delta == Decimal("1.40")
    assert result.summary.cvd_change == Decimal("1.40")
    assert result.summary.buy_volume == Decimal("2.80")
    assert result.summary.sell_volume == Decimal("1.40")


def test_kline_delta_cross_check_is_exact_decimal_arithmetic() -> None:
    assert kline_delta(volume=Decimal("0.60"), taker_buy_volume=Decimal("0.40")) == Decimal("0.20")
    assert kline_delta(volume=Decimal("7.00"), taker_buy_volume=Decimal("4.25")) == Decimal("1.50")

    result = compute_cvd(
        _fixture_week(),
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    for point in result.points:
        volume = point.buy_volume + point.sell_volume
        assert_kline_cross_check(point, volume=volume, taker_buy_volume=point.buy_volume)


def test_kline_cross_check_rejects_drift() -> None:
    trades = (_trade(1, "2.00", "70000.00", is_buyer_maker=False),)
    point = compute_cvd(
        trades,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=4),
    ).points[0]

    with pytest.raises(ValueError, match="kline"):
        assert_kline_cross_check(point, volume=Decimal("2.01"), taker_buy_volume=Decimal("2.00"))


def test_duplicate_trade_ids_are_rejected() -> None:
    trades = (
        _trade(1, "1.00", "70000.00", is_buyer_maker=False),
        _trade(1, "2.00", "70001.00", is_buyer_maker=False),
    )

    with pytest.raises(ValueError, match="duplicate"):
        compute_cvd(
            trades,
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


def test_trade_outside_window_is_rejected() -> None:
    trade = _trade(1, "1.00", "70000.00", is_buyer_maker=False, day=8)

    with pytest.raises(ValueError, match="outside"):
        compute_cvd(
            (trade,),
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


def test_symbol_mismatch_is_rejected() -> None:
    trade = _trade(1, "1.00", "70000.00", is_buyer_maker=False).model_copy(
        update={"symbol": "ETHUSDT"}
    )

    with pytest.raises(ValueError, match="symbol"):
        compute_cvd(
            (trade,),
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


def test_naive_window_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        compute_cvd(
            (),
            symbol="BTCUSDT",
            period="4h",
            window_start=datetime(2026, 10, 1),
            window_end=datetime(2026, 10, 2),
        )


def test_kline_delta_range_validation() -> None:
    with pytest.raises(ValueError, match="volume"):
        kline_delta(volume=Decimal("-1"), taker_buy_volume=Decimal("0"))
    with pytest.raises(ValueError, match="taker_buy_volume"):
        kline_delta(volume=Decimal("1"), taker_buy_volume=Decimal("2"))

    assert kline_delta(volume=Decimal("1"), taker_buy_volume=Decimal("0")) == Decimal("-1")
    assert kline_delta(volume=Decimal("1"), taker_buy_volume=Decimal("1")) == Decimal("1")


def test_zero_length_window_is_rejected() -> None:
    with pytest.raises(ValueError):
        compute_cvd(
            (),
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_START,
        )
