"""Deterministic CVD/delta metrics from taker-side aggTrade volumes."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from oft.domain.events import AggTrade
from oft.domain.metrics.delta import parse_period
from oft.domain.metrics.types import (
    CvdBar,
    CvdResult,
    CvdSummary,
    MetricWindow,
    Unit,
    validated_trades,
)

RATIO_QUANTUM = Decimal("0.00000001")


def taker_buy_ratio(buy_volume: Decimal, sell_volume: Decimal) -> Decimal | None:
    """Buy share of taker volume, quantized to 8 places; ``None`` at zero."""
    total = buy_volume + sell_volume
    if total == 0:
        return None
    return (buy_volume / total).quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def kline_delta(*, volume: Decimal, taker_buy_volume: Decimal) -> Decimal:
    """The kline identity ``delta = 2 * taker_buy_volume - volume``."""
    if volume < 0:
        raise ValueError("volume must be non-negative")
    if taker_buy_volume < 0 or taker_buy_volume > volume:
        raise ValueError("taker_buy_volume must be between zero and volume")
    return Decimal(2) * taker_buy_volume - volume


def assert_kline_cross_check(bar: CvdBar, *, volume: Decimal, taker_buy_volume: Decimal) -> None:
    """Fail when a bar's delta disagrees with the matching kline row."""
    expected = kline_delta(volume=volume, taker_buy_volume=taker_buy_volume)
    if bar.delta != expected:
        raise ValueError(
            f"kline cross-check failed for bar {bar.bar_start.isoformat()}: "
            f"bar delta {bar.delta} != kline delta {expected}"
        )


def compute_cvd(
    trades: Sequence[AggTrade],
    *,
    symbol: str,
    period: str,
    window_start: datetime,
    window_end: datetime,
    unit: Unit = "BASE",
) -> CvdResult:
    """Compute per-bar taker buy/sell volume, delta, and cumulative CVD.

    Bars are emitted only where trades exist; the window is half-open
    ``[window_start, window_end)``. Duplicate ids and out-of-window rows are
    rejected so a retried ingest can never inflate volume.
    """
    window = MetricWindow(window_start, window_end)
    duration = parse_period(period)
    ordered = validated_trades(trades, symbol=symbol, window=window)

    buys: dict[int, Decimal] = defaultdict(Decimal)
    sells: dict[int, Decimal] = defaultdict(Decimal)
    for trade in ordered:
        bucket = (trade.transact_time - window.start) // duration
        if trade.is_buyer_maker:
            sells[bucket] += trade.quantity
        else:
            buys[bucket] += trade.quantity

    cumulative = Decimal("0")
    points: list[CvdBar] = []
    for bucket in sorted(set(buys) | set(sells)):
        buy_volume = buys.get(bucket, Decimal("0"))
        sell_volume = sells.get(bucket, Decimal("0"))
        bar_delta = buy_volume - sell_volume
        cumulative += bar_delta
        bar_start = window.start + bucket * duration
        points.append(
            CvdBar(
                bar_start=bar_start,
                bar_end=min(bar_start + duration, window.end),
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                delta=bar_delta,
                cvd=cumulative,
                taker_buy_ratio=taker_buy_ratio(buy_volume, sell_volume),
            )
        )

    total_buy = sum(buys.values(), Decimal("0"))
    total_sell = sum(sells.values(), Decimal("0"))
    return CvdResult(
        symbol=symbol,
        period=period,
        unit=unit,
        window=window,
        points=tuple(points),
        summary=CvdSummary(
            delta=total_buy - total_sell,
            cvd_change=cumulative,
            buy_volume=total_buy,
            sell_volume=total_sell,
        ),
    )
