"""Deterministic delta and cumulative-volume-delta metrics.

Taker-side mapping (ADR-0008): a trade with ``is_buyer_maker=false`` was
initiated by the buyer (taker buy); ``is_buyer_maker=true`` was initiated by
the seller (taker sell).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from oft.domain.events import AggTrade

TakerSide = Literal["BUY", "SELL"]

_PERIODS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "1d": timedelta(days=1),
}


def parse_period(period: str) -> timedelta:
    """Translate a contract metric period into a duration."""
    try:
        return _PERIODS[period]
    except KeyError as exc:
        allowed = ", ".join(_PERIODS)
        raise ValueError(f"unsupported period {period!r}; expected one of {allowed}") from exc


def taker_side(*, is_buyer_maker: bool) -> TakerSide:
    """Return the aggressor side for one trade."""
    return "SELL" if is_buyer_maker else "BUY"


@dataclass(frozen=True)
class DeltaBar:
    """One bucketed delta/CVD point."""

    bar_start: datetime
    bar_end: datetime
    buy_volume: Decimal
    sell_volume: Decimal
    delta: Decimal
    cvd: Decimal
    trade_count: int


@dataclass(frozen=True)
class DeltaResult:
    """Window summary plus per-bar delta/CVD points."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    buy_volume: Decimal
    sell_volume: Decimal
    delta: Decimal
    cvd: Decimal
    trade_count: int
    bars: tuple[DeltaBar, ...]


def compute_delta(
    trades: tuple[AggTrade, ...] | list[AggTrade],
    *,
    symbol: str,
    period: str,
    window_start: datetime,
    window_end: datetime,
) -> DeltaResult:
    """Compute buy/sell volume, delta, and cumulative delta over a window.

    The window is half-open: ``window_start <= transact_time < window_end``.
    Duplicate aggTrade ids are rejected so a retried ingest cannot inflate
    volume.
    """
    duration = parse_period(period)
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    buys: dict[int, Decimal] = defaultdict(Decimal)
    sells: dict[int, Decimal] = defaultdict(Decimal)
    counts: dict[int, int] = defaultdict(int)
    seen_ids: set[int] = set()

    for trade in trades:
        if trade.symbol != symbol:
            raise ValueError(f"trade symbol {trade.symbol!r} does not match {symbol!r}")
        if trade.agg_trade_id in seen_ids:
            raise ValueError(f"duplicate agg_trade_id {trade.agg_trade_id}")
        seen_ids.add(trade.agg_trade_id)
        offset = trade.transact_time - window_start
        if offset < timedelta(0) or trade.transact_time >= window_end:
            raise ValueError(f"trade {trade.agg_trade_id} is outside the requested window")
        bucket = offset // duration
        if taker_side(is_buyer_maker=trade.is_buyer_maker) == "BUY":
            buys[bucket] += trade.quantity
        else:
            sells[bucket] += trade.quantity
        counts[bucket] += 1

    bars: list[DeltaBar] = []
    cumulative = Decimal("0")
    for bucket in sorted(counts):
        buy_volume = buys[bucket]
        sell_volume = sells[bucket]
        bar_delta = buy_volume - sell_volume
        cumulative += bar_delta
        bar_start = window_start + bucket * duration
        bar_end = min(bar_start + duration, window_end)
        bars.append(
            DeltaBar(
                bar_start=bar_start,
                bar_end=bar_end,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                delta=bar_delta,
                cvd=cumulative,
                trade_count=counts[bucket],
            )
        )

    total_buy = sum(buys.values(), Decimal("0"))
    total_sell = sum(sells.values(), Decimal("0"))
    return DeltaResult(
        symbol=symbol,
        period=period,
        window_start=window_start,
        window_end=window_end,
        buy_volume=total_buy,
        sell_volume=total_sell,
        delta=total_buy - total_sell,
        cvd=cumulative,
        trade_count=sum(counts.values()),
        bars=tuple(bars),
    )
