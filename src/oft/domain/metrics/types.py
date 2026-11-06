"""Exact-decimal value objects shared by the metric engine.

The metric core never uses binary floating point. Prices, quantities, deltas,
and volumes are :class:`decimal.Decimal`; contract decimals are rendered as
JSON strings. Only the two schema fields that are JSON numbers
(``taker_buy_ratio`` and ``value_area_pct``) stay Decimal until
:func:`contract_json` renders them as exact number literals.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from oft.domain.data_quality import DataQuality
from oft.domain.events import AggTrade

Unit = Literal["BASE", "QUOTE"]
RecordType = Literal["CVD_BAR", "VOLUME_PROFILE_BIN"]


def decimal_str(value: Decimal) -> str:
    """Render a finite Decimal as the contract's exact string form."""
    if not value.is_finite():
        raise ValueError("metric values must be finite decimals")
    return format(value, "f")


def contract_json(value: object) -> str:
    """Serialize a metric document deterministically without binary floats.

    ``Decimal`` becomes an unquoted JSON number, so ratio/percentage fields
    satisfy the schema while staying exact. Any ``float`` is rejected instead
    of silently entering the metric path.
    """
    return _render(value)


def _render(value: object) -> str:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("binary floats are not allowed in metric output")
    if isinstance(value, Mapping):
        items = ", ".join(f"{_render(str(key))}:{_render(item)}" for key, item in value.items())
        return "{" + items + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(_render(item) for item in value) + "]"
    raise TypeError(f"unsupported metric output type: {type(value).__name__}")


@dataclass(frozen=True)
class MetricWindow:
    """A half-open UTC window ``[start, end)``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("window_end must be after window_start")


@dataclass(frozen=True)
class CvdBar:
    """One bar's taker buy/sell volumes, delta, and cumulative delta."""

    bar_start: datetime
    bar_end: datetime
    buy_volume: Decimal
    sell_volume: Decimal
    delta: Decimal
    cvd: Decimal
    taker_buy_ratio: Decimal | None


@dataclass(frozen=True)
class CvdSummary:
    """Window-level delta totals."""

    delta: Decimal
    cvd_change: Decimal
    buy_volume: Decimal
    sell_volume: Decimal


@dataclass(frozen=True)
class DivergenceFlag:
    """A reserved divergence marker; v1 computes none."""

    bar_start: datetime
    kind: Literal["BULLISH", "BEARISH"]
    note: str


@dataclass(frozen=True)
class CvdResult:
    """Per-bar CVD points plus a window summary."""

    symbol: str
    period: str
    unit: Unit
    window: MetricWindow
    points: tuple[CvdBar, ...]
    summary: CvdSummary
    divergence_flags: tuple[DivergenceFlag, ...] = ()


@dataclass(frozen=True)
class VolumeBin:
    """One contiguous price bin and its taker-side volume split."""

    price_low: Decimal
    price_high: Decimal
    volume: Decimal
    buy_volume: Decimal
    sell_volume: Decimal
    is_poc: bool
    in_value_area: bool


@dataclass(frozen=True)
class VolumeProfile:
    """A histogram plus POC, value-area high/low, and notes."""

    symbol: str
    window: MetricWindow
    bin_size: Decimal
    value_area_pct: Decimal
    poc: Decimal
    vah: Decimal
    val: Decimal
    bins: tuple[VolumeBin, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricQuality:
    """The ADR-0008 quality stamp and its human-readable reasons."""

    data_quality: DataQuality
    quality_reasons: tuple[str, ...] = ()

    @classmethod
    def complete(cls) -> MetricQuality:
        return cls(data_quality=DataQuality.COMPLETE)

    @classmethod
    def missing(cls, reason: str) -> MetricQuality:
        return cls(data_quality=DataQuality.MISSING, quality_reasons=(reason,))

    @classmethod
    def partial(cls, reasons: Sequence[str]) -> MetricQuality:
        return cls(data_quality=DataQuality.PARTIAL, quality_reasons=tuple(reasons))


@dataclass(frozen=True)
class MetricRow:
    """One long-format Parquet row (a CVD bar or a volume-profile bin)."""

    record_type: RecordType
    bar_start: datetime | None = None
    bar_end: datetime | None = None
    buy_volume: Decimal | None = None
    sell_volume: Decimal | None = None
    delta: Decimal | None = None
    cvd: Decimal | None = None
    taker_buy_ratio: Decimal | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    volume: Decimal | None = None
    is_poc: bool | None = None
    in_value_area: bool | None = None


def validated_trades(
    trades: Sequence[AggTrade],
    *,
    symbol: str,
    window: MetricWindow,
) -> tuple[AggTrade, ...]:
    """Reject duplicates/out-of-window rows and return event-time order."""
    seen: set[int] = set()
    selected: list[AggTrade] = []
    for trade in trades:
        if trade.symbol != symbol:
            raise ValueError(f"trade symbol {trade.symbol!r} does not match {symbol!r}")
        if trade.agg_trade_id in seen:
            raise ValueError(f"duplicate agg_trade_id {trade.agg_trade_id}")
        seen.add(trade.agg_trade_id)
        if trade.transact_time < window.start or trade.transact_time >= window.end:
            raise ValueError(f"trade {trade.agg_trade_id} is outside the requested window")
        selected.append(trade)
    selected.sort(key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    return tuple(selected)
