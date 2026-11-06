"""Unit tests for the deterministic exact-decimal footprint metric."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from oft.domain.events import AggTrade
from oft.domain.metrics.footprint import compute_footprint
from oft.domain.metrics.types import MetricWindow

WINDOW_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(hours=8)


def _trade(
    trade_id: int,
    price: str,
    quantity: str,
    *,
    is_buyer_maker: bool,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=trade_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=trade_id * 10,
        last_trade_id=trade_id * 10,
        transact_time=datetime(2026, 10, day, hour, minute, second, tzinfo=UTC),
        is_buyer_maker=is_buyer_maker,
    )


def _compute(trades: tuple[AggTrade, ...], **kwargs: object) -> object:
    return compute_footprint(
        trades,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        tick_size=Decimal("0.10"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_bid_ask_volume_follow_is_buyer_maker() -> None:
    trades = (
        _trade(1, "70000.00", "2.00", is_buyer_maker=True),
        _trade(2, "70000.00", "3.00", is_buyer_maker=False),
        _trade(3, "70000.00", "1.00", is_buyer_maker=False),
    )

    result = _compute(trades)

    assert len(result.bars) == 1
    cell = result.bars[0].cells[0]
    assert cell.price == Decimal("70000.00")
    assert cell.bid_volume == Decimal("2.00")
    assert cell.ask_volume == Decimal("4.00")
    assert cell.delta == Decimal("2.00")
    assert cell.total_volume == Decimal("6.00")
    assert cell.imbalance_ratio == Decimal("2")
    assert cell.is_stacked_imbalance is False


def test_delta_is_ask_minus_bid() -> None:
    trades = (
        _trade(1, "70000.00", "5.00", is_buyer_maker=True),
        _trade(2, "70000.00", "2.00", is_buyer_maker=False),
    )

    cell = _compute(trades).bars[0].cells[0]

    assert cell.delta == Decimal("-3.00")
    assert cell.total_volume == Decimal("7.00")


def test_price_bucket_rounding_uses_tick_size_multiple() -> None:
    trades = (
        _trade(1, "70000.04", "1.00", is_buyer_maker=False),
        _trade(2, "70000.49", "2.00", is_buyer_maker=False),
        _trade(3, "70000.50", "3.00", is_buyer_maker=True),
    )

    result = _compute(trades, bucket_multiple=5)

    assert result.price_bucket == Decimal("0.50")
    assert [cell.price for cell in result.bars[0].cells] == [
        Decimal("70000.00"),
        Decimal("70000.50"),
    ]
    assert result.bars[0].cells[0].ask_volume == Decimal("3.00")
    assert result.bars[0].cells[1].bid_volume == Decimal("3.00")


def test_imbalance_ratio_side_and_threshold() -> None:
    trades = (
        _trade(1, "70000.00", "1.00", is_buyer_maker=True),
        _trade(2, "70000.00", "4.00", is_buyer_maker=False),
        _trade(3, "70001.00", "4.00", is_buyer_maker=True),
        _trade(4, "70001.00", "1.00", is_buyer_maker=False),
        _trade(5, "70002.00", "2.00", is_buyer_maker=True),
        _trade(6, "70002.00", "2.00", is_buyer_maker=False),
    )

    loose = _compute(trades, imbalance_threshold=Decimal("3"))
    tight = _compute(trades, imbalance_threshold=Decimal("5"))

    cells = {cell.price: cell for cell in loose.bars[0].cells}
    assert cells[Decimal("70000.00")].imbalance_ratio == Decimal("4")
    assert cells[Decimal("70001.00")].imbalance_ratio == Decimal("4")
    assert cells[Decimal("70002.00")].imbalance_ratio == Decimal("1")
    assert [top.side for top in loose.top_imbalances] == ["ASK", "BID"]
    assert [top.price for top in loose.top_imbalances] == [
        Decimal("70000.00"),
        Decimal("70001.00"),
    ]
    assert tight.top_imbalances == ()


def test_one_sided_cell_has_no_defined_ratio() -> None:
    trades = (_trade(1, "70000.00", "1.00", is_buyer_maker=False),)

    result = _compute(trades, imbalance_threshold=Decimal("2"))

    cell = result.bars[0].cells[0]
    assert cell.ask_volume == Decimal("1.00")
    assert cell.bid_volume == Decimal("0")
    assert cell.imbalance_ratio is None
    assert result.top_imbalances == ()


def test_stacked_imbalance_requires_contiguous_same_side_levels() -> None:
    trades = (
        _trade(1, "70000.00", "1.00", is_buyer_maker=True),
        _trade(2, "70000.00", "5.00", is_buyer_maker=False),
        _trade(3, "70000.10", "1.00", is_buyer_maker=True),
        _trade(4, "70000.10", "5.00", is_buyer_maker=False),
        _trade(5, "70000.20", "1.00", is_buyer_maker=True),
        _trade(6, "70000.20", "5.00", is_buyer_maker=False),
        # A price gap breaks the stacked run.
        _trade(7, "70000.40", "1.00", is_buyer_maker=True),
        _trade(8, "70000.40", "5.00", is_buyer_maker=False),
        # Two adjacent levels are a run only when the minimum is two.
        _trade(9, "70000.60", "1.00", is_buyer_maker=True),
        _trade(10, "70000.60", "5.00", is_buyer_maker=False),
        _trade(11, "70000.70", "1.00", is_buyer_maker=True),
        _trade(12, "70000.70", "5.00", is_buyer_maker=False),
    )

    default = _compute(trades)
    pair = _compute(trades, stacked_min_levels=2)

    default_flags = {cell.price: cell.is_stacked_imbalance for cell in default.bars[0].cells}
    pair_flags = {cell.price: cell.is_stacked_imbalance for cell in pair.bars[0].cells}
    assert default_flags[Decimal("70000.00")] is True
    assert default_flags[Decimal("70000.10")] is True
    assert default_flags[Decimal("70000.20")] is True
    assert default_flags[Decimal("70000.40")] is False
    assert default_flags[Decimal("70000.60")] is False
    assert default_flags[Decimal("70000.70")] is False
    assert pair_flags[Decimal("70000.60")] is True
    assert pair_flags[Decimal("70000.70")] is True


def test_top_imbalances_are_sorted_by_ratio_then_bar_and_price() -> None:
    trades = (
        _trade(1, "70002.00", "1.00", is_buyer_maker=True),
        _trade(2, "70002.00", "5.00", is_buyer_maker=False),
        _trade(3, "70000.00", "1.00", is_buyer_maker=True),
        _trade(4, "70000.00", "5.00", is_buyer_maker=False),
        _trade(5, "70001.00", "1.00", is_buyer_maker=True),
        _trade(6, "70001.00", "3.00", is_buyer_maker=False),
        # A later bar with the strongest ratio must sort first.
        _trade(7, "70000.00", "1.00", is_buyer_maker=True, hour=5),
        _trade(8, "70000.00", "6.00", is_buyer_maker=False, hour=5),
    )

    result = _compute(trades, top_imbalances=2)

    assert [(top.bar_start.hour, top.price, top.ratio) for top in result.top_imbalances] == [
        (4, Decimal("70000.00"), Decimal("6")),
        (0, Decimal("70000.00"), Decimal("5")),
    ]
    limited = _compute(trades, top_imbalances=1)
    assert len(limited.top_imbalances) == 1


def test_bars_are_half_open_periods_and_only_occupied_cells_are_emitted() -> None:
    trades = (
        _trade(1, "70000.00", "1.00", is_buyer_maker=False, hour=3, minute=59, second=59),
        _trade(2, "70001.00", "1.00", is_buyer_maker=True, hour=4, minute=0, second=0),
    )

    result = _compute(trades)

    assert len(result.bars) == 2
    assert result.bars[0].bar_start == WINDOW_START
    assert result.bars[0].bar_end == WINDOW_START + timedelta(hours=4)
    assert result.bars[1].bar_start == WINDOW_START + timedelta(hours=4)
    assert [cell.price for cell in result.bars[0].cells] == [Decimal("70000.00")]
    assert [cell.price for cell in result.bars[1].cells] == [Decimal("70001.00")]
    assert result.window == MetricWindow(WINDOW_START, WINDOW_END)


def test_empty_trades_produce_an_empty_footprint() -> None:
    result = _compute(())

    assert result.bars == ()
    assert result.top_imbalances == ()
    assert result.price_bucket == Decimal("0.10")
    assert result.imbalance_threshold == Decimal("3")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tick_size": Decimal("0")},
        {"tick_size": Decimal("-0.10")},
        {"bucket_multiple": 0},
        {"bucket_multiple": True},
        {"imbalance_threshold": Decimal("1")},
        {"stacked_min_levels": 1},
        {"top_imbalances": -1},
    ],
)
def test_invalid_footprint_parameters_are_rejected(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "trades": (),
        "symbol": "BTCUSDT",
        "period": "4h",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "tick_size": Decimal("0.10"),
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        compute_footprint(**values)  # type: ignore[arg-type]


def test_footprint_rejects_duplicate_out_of_window_and_mismatched_trades() -> None:
    duplicate = _trade(1, "70000.00", "1.00", is_buyer_maker=False)
    with pytest.raises(ValueError, match="duplicate"):
        _compute((duplicate, duplicate))

    outside = _trade(2, "70000.00", "1.00", is_buyer_maker=False, day=2)
    with pytest.raises(ValueError, match="outside"):
        _compute((outside,))

    mismatched = _trade(3, "70000.00", "1.00", is_buyer_maker=False).model_copy(
        update={"symbol": "ETHUSDT"}
    )
    with pytest.raises(ValueError, match="symbol"):
        _compute((mismatched,))
