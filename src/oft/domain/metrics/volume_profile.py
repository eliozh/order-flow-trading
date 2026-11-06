"""Exact-decimal volume profile: histogram, POC, and the 70% value area.

Bin edges are computed with Decimal floor division, so a price on a boundary
never leaks binary-float drift. The tie-breaking rules are fixed and recorded
in ``metric_version``: POC ties go to the lowest price, and the value area is
the smallest contiguous range, preferring ranges that contain the POC and then
the lowest price range.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from oft.domain.events import AggTrade
from oft.domain.metrics.types import (
    MetricWindow,
    VolumeBin,
    VolumeProfile,
    validated_trades,
)

DEFAULT_VALUE_AREA_PCT = Decimal("70")
_MIN_VALUE_AREA = Decimal("0")


def _validate(tick_size: Decimal, bin_size_multiple: int, value_area_pct: Decimal) -> None:
    if not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite decimal")
    if isinstance(bin_size_multiple, bool) or bin_size_multiple < 1:
        raise ValueError("bin_size_multiple must be an integer >= 1")
    if not value_area_pct.is_finite() or not (_MIN_VALUE_AREA < value_area_pct <= Decimal("100")):
        raise ValueError("value_area_pct must be in (0, 100]")


def _bin_index(price: Decimal, bin_size: Decimal) -> int:
    """Floor price to its bin; Decimal division keeps boundaries exact."""
    return int(price // bin_size)


def _better_value_area(candidate: tuple[int, int], best: tuple[int, int], poc_index: int) -> bool:
    candidate_length = candidate[1] - candidate[0]
    best_length = best[1] - best[0]
    if candidate_length != best_length:
        return candidate_length < best_length
    candidate_has_poc = candidate[0] <= poc_index <= candidate[1]
    best_has_poc = best[0] <= poc_index <= best[1]
    if candidate_has_poc != best_has_poc:
        return candidate_has_poc
    return candidate[0] < best[0]


def _value_area(
    volumes: Sequence[Decimal], poc_index: int, value_area_pct: Decimal
) -> tuple[int, int]:
    """Smallest contiguous bin range whose volume reaches the threshold."""
    total = sum(volumes, Decimal("0"))
    target = total * value_area_pct / Decimal("100")
    if target <= _MIN_VALUE_AREA:
        return poc_index, poc_index

    best: tuple[int, int] | None = None
    left = 0
    running = Decimal("0")
    for right, volume in enumerate(volumes):
        running += volume
        while left <= right and running >= target:
            # Semantic constraint (recorded in metric_version): the value area
            # is the smallest contiguous range that reaches the threshold AND
            # contains the POC. A smaller/higher-volume range that excludes the
            # POC is never acceptable.
            if left <= poc_index <= right:
                candidate = (left, right)
                if best is None or _better_value_area(candidate, best, poc_index):
                    best = candidate
            running -= volumes[left]
            left += 1
    if best is None:  # pragma: no cover - target is <= total by construction
        return poc_index, poc_index
    return best


def compute_volume_profile(
    trades: Sequence[AggTrade],
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    tick_size: Decimal,
    bin_size_multiple: int = 1,
    value_area_pct: Decimal = DEFAULT_VALUE_AREA_PCT,
) -> VolumeProfile:
    """Build the price histogram and its POC/value-area summary."""
    _validate(tick_size, bin_size_multiple, value_area_pct)
    window = MetricWindow(window_start, window_end)
    ordered = validated_trades(trades, symbol=symbol, window=window)
    bin_size = tick_size * bin_size_multiple

    if not ordered:
        return VolumeProfile(
            symbol=symbol,
            window=window,
            bin_size=bin_size,
            value_area_pct=value_area_pct,
            poc=Decimal("0"),
            vah=Decimal("0"),
            val=Decimal("0"),
            bins=(),
            notes=("NO_TRADES",),
        )

    buys: dict[int, Decimal] = {}
    sells: dict[int, Decimal] = {}
    for trade in ordered:
        index = _bin_index(trade.price, bin_size)
        if trade.is_buyer_maker:
            sells[index] = sells.get(index, Decimal("0")) + trade.quantity
        else:
            buys[index] = buys.get(index, Decimal("0")) + trade.quantity

    first_index = min(buys.keys() | sells.keys())
    last_index = max(buys.keys() | sells.keys())
    indices = list(range(first_index, last_index + 1))
    # Collapse to one row per bin so value-area contiguity is meaningful.
    volumes: list[Decimal] = []
    for index in indices:
        buy_volume = buys.get(index, Decimal("0"))
        sell_volume = sells.get(index, Decimal("0"))
        volumes.append(buy_volume + sell_volume)

    poc_index = 0
    for position in range(1, len(volumes)):
        if volumes[position] > volumes[poc_index]:
            poc_index = position

    area_start, area_end = _value_area(volumes, poc_index, value_area_pct)
    bins = tuple(
        VolumeBin(
            price_low=Decimal(index) * bin_size,
            price_high=Decimal(index) * bin_size + bin_size,
            volume=volumes[position],
            buy_volume=buys.get(index, Decimal("0")),
            sell_volume=sells.get(index, Decimal("0")),
            is_poc=position == poc_index,
            in_value_area=area_start <= position <= area_end,
        )
        for position, index in enumerate(indices)
    )
    return VolumeProfile(
        symbol=symbol,
        window=window,
        bin_size=bin_size,
        value_area_pct=value_area_pct,
        poc=bins[poc_index].price_low,
        vah=bins[area_end].price_high,
        val=bins[area_start].price_low,
        bins=bins,
        notes=(),
    )
