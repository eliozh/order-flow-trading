"""Unit tests for taker-side classification and delta/CVD computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from oft.domain.events import AggTrade
from oft.domain.metrics.delta import (
    compute_delta,
    parse_period,
    taker_side,
)

WINDOW_START = datetime(2025, 11, 5, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2025, 11, 5, 4, 0, tzinfo=UTC)


def _trade(
    trade_id: int,
    quantity: str,
    *,
    is_buyer_maker: bool,
    offset_seconds: int = 0,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=trade_id,
        price=Decimal("96000.00"),
        quantity=Decimal(quantity),
        first_trade_id=trade_id * 10,
        last_trade_id=trade_id * 10,
        transact_time=WINDOW_START + timedelta(seconds=offset_seconds),
        is_buyer_maker=is_buyer_maker,
    )


def _fixture_trades() -> tuple[AggTrade, ...]:
    """Six trades matching tests/fixtures/market/btcusdt_agg-trades_sample.csv."""
    return (
        _trade(1, "1.5", is_buyer_maker=False, offset_seconds=0),
        _trade(2, "0.5", is_buyer_maker=True, offset_seconds=1),
        _trade(3, "2.0", is_buyer_maker=False, offset_seconds=2),
        _trade(4, "1.25", is_buyer_maker=True, offset_seconds=3),
        _trade(5, "0.75", is_buyer_maker=False, offset_seconds=4),
        _trade(6, "1.0", is_buyer_maker=True, offset_seconds=5),
    )


def test_is_buyer_maker_false_is_taker_buy() -> None:
    assert taker_side(is_buyer_maker=False) == "BUY"


def test_is_buyer_maker_true_is_taker_sell() -> None:
    assert taker_side(is_buyer_maker=True) == "SELL"


@pytest.mark.parametrize(
    ("period", "seconds"),
    [
        ("1m", 60),
        ("5m", 300),
        ("15m", 900),
        ("1h", 3600),
        ("4h", 14400),
        ("8h", 28800),
        ("1d", 86400),
    ],
)
def test_parse_period(period: str, seconds: int) -> None:
    assert parse_period(period) == timedelta(seconds=seconds)


def test_parse_period_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        parse_period("3h")


def test_fixture_delta_matches_golden_value() -> None:
    result = compute_delta(
        _fixture_trades(),
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result.buy_volume == Decimal("4.25")
    assert result.sell_volume == Decimal("2.75")
    assert result.delta == Decimal("1.50")
    assert result.cvd == Decimal("1.50")
    assert result.trade_count == 6
    assert len(result.bars) == 1
    bar = result.bars[0]
    assert bar.buy_volume == Decimal("4.25")
    assert bar.sell_volume == Decimal("2.75")
    assert bar.delta == Decimal("1.50")
    assert bar.cvd == Decimal("1.50")
    assert bar.bar_start == WINDOW_START
    assert bar.bar_end == WINDOW_START + timedelta(hours=4)


def test_cvd_accumulates_across_bars() -> None:
    trades = (
        _trade(1, "2", is_buyer_maker=False, offset_seconds=0),
        _trade(2, "0.5", is_buyer_maker=True, offset_seconds=60),
        _trade(3, "0.25", is_buyer_maker=True, offset_seconds=4 * 3600),
        _trade(4, "1", is_buyer_maker=False, offset_seconds=4 * 3600 + 60),
    )

    result = compute_delta(
        trades,
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(hours=8),
    )

    assert len(result.bars) == 2
    assert result.bars[0].delta == Decimal("1.5")
    assert result.bars[0].cvd == Decimal("1.5")
    assert result.bars[1].delta == Decimal("0.75")
    assert result.bars[1].cvd == Decimal("2.25")
    assert result.delta == Decimal("2.25")
    assert result.cvd == Decimal("2.25")


def test_empty_window_returns_zero_summary() -> None:
    result = compute_delta(
        (),
        symbol="BTCUSDT",
        period="4h",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result.buy_volume == Decimal("0")
    assert result.sell_volume == Decimal("0")
    assert result.delta == Decimal("0")
    assert result.cvd == Decimal("0")
    assert result.trade_count == 0
    assert result.bars == ()


def test_trade_outside_window_is_rejected() -> None:
    trade = _trade(1, "1", is_buyer_maker=False, offset_seconds=5 * 3600)

    with pytest.raises(ValueError):
        compute_delta(
            (trade,),
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


def test_duplicate_trade_ids_are_rejected() -> None:
    trades = (
        _trade(1, "1", is_buyer_maker=False, offset_seconds=0),
        _trade(1, "2", is_buyer_maker=False, offset_seconds=1),
    )

    with pytest.raises(ValueError):
        compute_delta(
            trades,
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


def test_symbol_mismatch_is_rejected() -> None:
    trade = _trade(1, "1", is_buyer_maker=False)
    other = trade.model_copy(update={"symbol": "ETHUSDT"})

    with pytest.raises(ValueError):
        compute_delta(
            (other,),
            symbol="BTCUSDT",
            period="4h",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
