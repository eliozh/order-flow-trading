"""Unit tests for the exact-decimal volume profile (POC/VAH/VAL)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from oft.domain.events import AggTrade
from oft.domain.metrics.types import MetricWindow
from oft.domain.metrics.volume_profile import compute_volume_profile

WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


def _trade(
    trade_id: int,
    quantity: str,
    price: str,
    *,
    is_buyer_maker: bool,
    day: int = 1,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=trade_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=trade_id * 10,
        last_trade_id=trade_id * 10,
        transact_time=datetime(2026, 10, day, 12, 0, tzinfo=UTC),
        is_buyer_maker=is_buyer_maker,
    )


def _fixture_week() -> tuple[AggTrade, ...]:
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


def test_week_fixture_histogram_poc_and_value_area_are_exact() -> None:
    result = compute_volume_profile(
        _fixture_week(),
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
    )

    assert result.window == MetricWindow(WINDOW_START, WINDOW_END)
    assert result.bin_size == Decimal("0.10")
    assert result.value_area_pct == Decimal("70")
    assert len(result.bins) == 21  # 0.10 bins span 20 ticks between prices
    assert result.poc == Decimal("70002.00")
    assert result.vah == Decimal("70002.10")
    assert result.val == Decimal("70001.00")
    assert result.notes == ()

    low = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("70000.00"))
    middle = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("70001.00"))
    high = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("70002.00"))
    empty = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("70000.10"))
    assert (low.price_low, low.price_high) == (Decimal("70000.00"), Decimal("70000.10"))
    assert (low.buy_volume, low.sell_volume, low.volume) == (
        Decimal("0.70"),
        Decimal("0"),
        Decimal("0.70"),
    )
    assert (middle.buy_volume, middle.sell_volume, middle.volume) == (
        Decimal("0"),
        Decimal("1.40"),
        Decimal("1.40"),
    )
    assert (high.buy_volume, high.sell_volume, high.volume) == (
        Decimal("2.10"),
        Decimal("0"),
        Decimal("2.10"),
    )
    assert high.is_poc and not low.is_poc and not middle.is_poc
    assert empty.volume == Decimal("0")
    assert high.in_value_area and middle.in_value_area
    assert not low.in_value_area and not empty.in_value_area


def test_value_area_is_the_smallest_contiguous_range_containing_threshold() -> None:
    trades = (
        _trade(1, "3", "100.00", is_buyer_maker=False),
        _trade(2, "2", "101.00", is_buyer_maker=False),
        _trade(3, "3", "102.00", is_buyer_maker=False),
        _trade(4, "2", "103.00", is_buyer_maker=False),
    )

    result = compute_volume_profile(
        trades,
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("1"),
    )

    # Total 10, target 7. Length 2 never reaches 7; both length-3 windows do.
    # The tie is broken toward the window containing the POC (lowest price on
    # a POC tie), so bins 0..2 win over bins 1..3.
    assert result.poc == Decimal("100")
    assert result.val == Decimal("100")
    assert result.vah == Decimal("103")
    assert [bin_.in_value_area for bin_ in result.bins] == [True, True, True, False]


def test_poc_tie_breaks_to_the_lowest_price() -> None:
    trades = (
        _trade(1, "2", "100.00", is_buyer_maker=False),
        _trade(2, "3", "101.00", is_buyer_maker=False),
        _trade(3, "3", "102.00", is_buyer_maker=False),
    )

    result = compute_volume_profile(
        trades,
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("1"),
    )

    assert result.poc == Decimal("101")
    assert [bin_.is_poc for bin_ in result.bins] == [False, True, False]


def test_bin_size_is_tick_size_times_the_configured_multiple() -> None:
    result = compute_volume_profile(
        _fixture_week(),
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
        bin_size_multiple=5,
    )

    assert result.bin_size == Decimal("0.50")
    assert result.bins[0].price_low == Decimal("70000.00")
    assert result.bins[-1].price_low == Decimal("70002.00")


def test_price_exactly_on_a_bin_boundary_has_no_float_drift() -> None:
    trades = (
        _trade(1, "1", "1.23", is_buyer_maker=False),
        _trade(2, "2", "1.25", is_buyer_maker=False),
        _trade(3, "3", "0.3", is_buyer_maker=False),
    )

    result = compute_volume_profile(
        trades,
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.01"),
        bin_size_multiple=5,
    )

    assert result.bin_size == Decimal("0.05")
    assert result.bins[0].price_low == Decimal("0.30")
    assert result.bins[0].price_high == Decimal("0.35")
    boundary = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("1.20"))
    assert boundary.price_high == Decimal("1.25")
    assert boundary.volume == Decimal("1")
    next_bin = next(bin_ for bin_ in result.bins if bin_.price_low == Decimal("1.25"))
    assert next_bin.price_high == Decimal("1.30")
    assert next_bin.volume == Decimal("2")


def test_value_area_percentage_is_configurable() -> None:
    nine = compute_volume_profile(
        _fixture_week(),
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("100"),
    )
    assert all(bin_.in_value_area for bin_ in nine.bins)
    assert nine.val == Decimal("70000.00")
    assert nine.vah == Decimal("70002.10")

    half = compute_volume_profile(
        _fixture_week(),
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
        value_area_pct=Decimal("50"),
    )
    half_low = next(bin_ for bin_ in half.bins if bin_.price_low == Decimal("70000.00"))
    half_middle = next(bin_ for bin_ in half.bins if bin_.price_low == Decimal("70001.00"))
    half_high = next(bin_ for bin_ in half.bins if bin_.price_low == Decimal("70002.00"))
    assert not half_low.in_value_area and not half_middle.in_value_area
    assert half_high.in_value_area
    assert half.val == Decimal("70002.00")
    assert half.vah == Decimal("70002.10")


def test_empty_window_returns_an_explicit_empty_profile() -> None:
    result = compute_volume_profile(
        (),
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
    )

    assert result.bins == ()
    assert result.poc == Decimal("0")
    assert result.vah == Decimal("0")
    assert result.val == Decimal("0")
    assert result.notes == ("NO_TRADES",)


@pytest.mark.parametrize(
    ("tick_size", "multiple", "pct"),
    [
        (Decimal("0"), 1, Decimal("70")),
        (Decimal("-0.10"), 1, Decimal("70")),
        (Decimal("0.10"), 0, Decimal("70")),
        (Decimal("0.10"), -1, Decimal("70")),
        (Decimal("0.10"), 1, Decimal("0")),
        (Decimal("0.10"), 1, Decimal("100.01")),
    ],
)
def test_invalid_parameters_are_rejected(tick_size: Decimal, multiple: int, pct: Decimal) -> None:
    with pytest.raises(ValueError):
        compute_volume_profile(
            (),
            symbol="BTCUSDT",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=tick_size,
            bin_size_multiple=multiple,
            value_area_pct=pct,
        )


def test_trade_outside_window_is_rejected() -> None:
    trade = _trade(1, "1", "70000.00", is_buyer_maker=False, day=8)

    with pytest.raises(ValueError, match="outside"):
        compute_volume_profile(
            (trade,),
            symbol="BTCUSDT",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=Decimal("0.10"),
        )


def test_duplicate_trade_ids_are_rejected() -> None:
    trades = (
        _trade(1, "1", "70000.00", is_buyer_maker=False),
        _trade(1, "2", "70001.00", is_buyer_maker=False),
    )

    with pytest.raises(ValueError, match="duplicate"):
        compute_volume_profile(
            trades,
            symbol="BTCUSDT",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=Decimal("0.10"),
        )


def test_volume_profile_uses_only_the_requested_window() -> None:
    inside = _trade(1, "1", "70000.00", is_buyer_maker=False, day=2)
    outside = _trade(2, "5", "90000.00", is_buyer_maker=False, day=20)

    with pytest.raises(ValueError, match="outside"):
        compute_volume_profile(
            (inside, outside),
            symbol="BTCUSDT",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            tick_size=Decimal("0.10"),
        )


def test_value_area_prefers_the_lowest_start_on_equal_length_ties() -> None:
    trades = tuple(
        _trade(index + 1, quantity, price, is_buyer_maker=False)
        for index, (quantity, price) in enumerate(
            (("2", "1.00"), ("5", "2.00"), ("2", "3.00"), ("1", "4.00"))
        )
    )

    profile = compute_volume_profile(
        trades,
        symbol="BTCUSDT",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("1"),
    )

    areas = [bin_.in_value_area for bin_ in profile.bins]
    assert areas[:2] == [True, True]
    assert areas[2:] == [False, False]
    assert profile.val == Decimal("1")
    assert profile.vah == Decimal("3")
