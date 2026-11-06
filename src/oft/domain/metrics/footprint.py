"""Exact-decimal footprint: per-bar, per-price taker volume and imbalance.

Taker-side mapping follows ADR-0008 and the CVD convention: a trade with
``is_buyer_maker=true`` was initiated by the seller (it hit the bid), so it is
``bid_volume``; ``is_buyer_maker=false`` was initiated by the buyer (it lifted
the ask), so it is ``ask_volume``. Price cells are floored to a bucket built
from the instrument tick size and a configured multiple, using ``Decimal``
floor division so a price on a boundary never leaks binary-float drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from oft.domain.events import AggTrade
from oft.domain.metrics.delta import parse_period
from oft.domain.metrics.types import MetricWindow, decimal_str, validated_trades
from oft.domain.time import utc_timestamp

DEFAULT_IMBALANCE_THRESHOLD = Decimal("3")
DEFAULT_STACKED_MIN_LEVELS = 3
DEFAULT_TOP_IMBALANCES = 10
RATIO_QUANTUM = Decimal("0.00000001")
FootprintSide = Literal["BID", "ASK"]


@dataclass(frozen=True)
class FootprintCell:
    """One price bucket inside one bar."""

    price: Decimal
    bid_volume: Decimal
    ask_volume: Decimal
    delta: Decimal
    total_volume: Decimal
    imbalance_ratio: Decimal | None
    is_stacked_imbalance: bool

    def to_contract(self) -> dict[str, object]:
        return {
            "price": decimal_str(self.price),
            "bid_volume": decimal_str(self.bid_volume),
            "ask_volume": decimal_str(self.ask_volume),
            "delta": decimal_str(self.delta),
            "total_volume": decimal_str(self.total_volume),
            "imbalance_ratio": self.imbalance_ratio,
            "is_stacked_imbalance": self.is_stacked_imbalance,
        }


@dataclass(frozen=True)
class FootprintBar:
    """All occupied price cells that traded inside one period bar."""

    bar_start: datetime
    bar_end: datetime
    cells: tuple[FootprintCell, ...]

    def to_contract(self) -> dict[str, object]:
        return {
            "bar_start": utc_timestamp(self.bar_start),
            "bar_end": utc_timestamp(self.bar_end),
            "cells": [cell.to_contract() for cell in self.cells],
        }


@dataclass(frozen=True)
class TopImbalance:
    """One of the strongest imbalance cells across the window."""

    bar_start: datetime
    price: Decimal
    side: FootprintSide
    ratio: Decimal

    def to_contract(self) -> dict[str, object]:
        return {
            "bar_start": utc_timestamp(self.bar_start),
            "price": decimal_str(self.price),
            "side": self.side,
            "ratio": self.ratio,
        }


@dataclass(frozen=True)
class FootprintResult:
    """A footprint document section: bars, cells, and top imbalances."""

    symbol: str
    period: str
    window: MetricWindow
    price_bucket: Decimal
    imbalance_threshold: Decimal
    bars: tuple[FootprintBar, ...]
    top_imbalances: tuple[TopImbalance, ...]

    def to_contract(self) -> dict[str, object]:
        return {
            "price_bucket": decimal_str(self.price_bucket),
            "imbalance_threshold": self.imbalance_threshold,
            "bars": [bar.to_contract() for bar in self.bars],
            "top_imbalances": [top.to_contract() for top in self.top_imbalances],
        }


def _validate(
    tick_size: Decimal,
    bucket_multiple: int,
    imbalance_threshold: Decimal,
    stacked_min_levels: int,
    top_imbalances: int,
) -> None:
    if not isinstance(tick_size, Decimal) or not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite decimal")
    if isinstance(bucket_multiple, bool) or not isinstance(bucket_multiple, int):
        raise ValueError("bucket_multiple must be an integer >= 1")
    if bucket_multiple < 1:
        raise ValueError("bucket_multiple must be an integer >= 1")
    if (
        not isinstance(imbalance_threshold, Decimal)
        or not imbalance_threshold.is_finite()
        or imbalance_threshold <= 1
    ):
        raise ValueError("imbalance_threshold must be a finite decimal greater than 1")
    if (
        isinstance(stacked_min_levels, bool)
        or not isinstance(stacked_min_levels, int)
        or stacked_min_levels < 2
    ):
        raise ValueError("stacked_min_levels must be an integer >= 2")
    if isinstance(top_imbalances, bool) or not isinstance(top_imbalances, int):
        raise ValueError("top_imbalances must be an integer >= 0")
    if top_imbalances < 0:
        raise ValueError("top_imbalances must be an integer >= 0")


def _ratio(bid_volume: Decimal, ask_volume: Decimal) -> Decimal | None:
    """Dominant/opposite taker volume; ``None`` when one side is absent."""
    smaller = min(bid_volume, ask_volume)
    if smaller == 0:
        return None
    return (max(bid_volume, ask_volume) / smaller).quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def _side(bid_volume: Decimal, ask_volume: Decimal) -> FootprintSide:
    return "BID" if bid_volume >= ask_volume else "ASK"


def _is_imbalanced(ratio: Decimal | None, threshold: Decimal) -> bool:
    return ratio is not None and ratio >= threshold


def _stacked_indices(
    indices: Sequence[int],
    sides: dict[int, FootprintSide],
    imbalanced: dict[int, bool],
    stacked_min_levels: int,
) -> set[int]:
    stacked: set[int] = set()
    run: list[tuple[int, FootprintSide]] = []

    def flush() -> None:
        if len(run) >= stacked_min_levels:
            stacked.update(index for index, _ in run)

    for index in indices:
        if not imbalanced[index]:
            flush()
            run = []
            continue
        side = sides[index]
        if run and run[-1][0] + 1 == index and run[-1][1] == side:
            run.append((index, side))
        else:
            flush()
            run = [(index, side)]
    flush()
    return stacked


def compute_footprint(
    trades: Sequence[AggTrade],
    *,
    symbol: str,
    period: str,
    window_start: datetime,
    window_end: datetime,
    tick_size: Decimal,
    bucket_multiple: int = 1,
    imbalance_threshold: Decimal = DEFAULT_IMBALANCE_THRESHOLD,
    stacked_min_levels: int = DEFAULT_STACKED_MIN_LEVELS,
    top_imbalances: int = DEFAULT_TOP_IMBALANCES,
) -> FootprintResult:
    """Aggregate taker volume per bar and price bucket, then flag imbalances.

    A cell's imbalance ratio is ``dominant_volume / opposite_volume`` when both
    sides traded; a one-sided cell has no defined ratio. A stacked imbalance is
    a contiguous same-side run of at least ``stacked_min_levels`` imbalanced
    price buckets inside one bar. ``top_imbalances`` is ordered by ratio
    descending, then bar start, price, and side.
    """
    _validate(
        tick_size,
        bucket_multiple,
        imbalance_threshold,
        stacked_min_levels,
        top_imbalances,
    )
    window = MetricWindow(window_start, window_end)
    duration = parse_period(period)
    ordered = validated_trades(trades, symbol=symbol, window=window)
    bucket_size = tick_size * bucket_multiple

    bids: dict[tuple[int, int], Decimal] = {}
    asks: dict[tuple[int, int], Decimal] = {}
    for trade in ordered:
        bar_bucket = (trade.transact_time - window.start) // duration
        price_index = int(trade.price // bucket_size)
        key = (bar_bucket, price_index)
        if trade.is_buyer_maker:
            bids[key] = bids.get(key, Decimal("0")) + trade.quantity
        else:
            asks[key] = asks.get(key, Decimal("0")) + trade.quantity

    bars: list[FootprintBar] = []
    all_imbalances: list[TopImbalance] = []
    for bar_bucket in sorted({bar for bar, _ in bids} | {bar for bar, _ in asks}):
        indices = sorted(index for bar, index in {*bids, *asks} if bar == bar_bucket)
        bid_volumes = {index: bids.get((bar_bucket, index), Decimal("0")) for index in indices}
        ask_volumes = {index: asks.get((bar_bucket, index), Decimal("0")) for index in indices}
        ratios = {index: _ratio(bid_volumes[index], ask_volumes[index]) for index in indices}
        sides = {index: _side(bid_volumes[index], ask_volumes[index]) for index in indices}
        imbalanced = {
            index: _is_imbalanced(ratios[index], imbalance_threshold) for index in indices
        }
        stacked = _stacked_indices(indices, sides, imbalanced, stacked_min_levels)

        bar_start = window.start + bar_bucket * duration
        bar_end = min(bar_start + duration, window.end)
        cells: list[FootprintCell] = []
        for index in indices:
            bid_volume = bid_volumes[index]
            ask_volume = ask_volumes[index]
            cells.append(
                FootprintCell(
                    price=Decimal(index) * bucket_size,
                    bid_volume=bid_volume,
                    ask_volume=ask_volume,
                    delta=ask_volume - bid_volume,
                    total_volume=bid_volume + ask_volume,
                    imbalance_ratio=ratios[index],
                    is_stacked_imbalance=index in stacked,
                )
            )
            if imbalanced[index]:
                ratio = ratios[index]
                assert ratio is not None
                all_imbalances.append(
                    TopImbalance(
                        bar_start=bar_start,
                        price=Decimal(index) * bucket_size,
                        side=sides[index],
                        ratio=ratio,
                    )
                )
        bars.append(FootprintBar(bar_start=bar_start, bar_end=bar_end, cells=tuple(cells)))

    all_imbalances.sort(key=lambda top: (-top.ratio, top.bar_start, top.price, top.side))
    return FootprintResult(
        symbol=symbol,
        period=period,
        window=window,
        price_bucket=bucket_size,
        imbalance_threshold=imbalance_threshold,
        bars=tuple(bars),
        top_imbalances=tuple(all_imbalances[:top_imbalances]),
    )
