"""UTC-aligned OHLCV bars built deterministically from normalized aggTrades.

Every bar boundary is a floor to a UTC epoch multiple, so 4h bars open at
00/04/08/12/16/20 UTC and 8h bars at 00/08/16 UTC regardless of the host
timezone. Bar values are exact :class:`~decimal.Decimal` amounts; binary
floats are never constructed. Time is supplied by the caller (``closed_at``),
which lets replay and tests avoid the wall clock entirely.

No-lookahead rule: a bar is emitted only when it is fully closed at
``closed_at``. Trades at or after that instant never contribute to any bar.
Missing bars are simply absent from the output; the module reports their slots
so a downstream consumer can stamp PARTIAL/MISSING instead of inventing data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from oft.domain.data_quality import DataQuality
from oft.domain.events import AggTrade
from oft.domain.metrics.delta import parse_period
from oft.domain.metrics.types import decimal_str
from oft.domain.metrics.version import MetricVersion, build_metric_version
from oft.domain.time import utc_timestamp

BAR_PERIODS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "8h", "1d")
SourceMode = Literal["LIVE", "BACKFILL", "REPLAY", "DERIVED"]
SOURCE_MODES: frozenset[str] = frozenset({"LIVE", "BACKFILL", "REPLAY", "DERIVED"})

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MICROSECOND = timedelta(microseconds=1)


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _duration(period: str) -> timedelta:
    return parse_period(period)


def bar_start_for(instant: datetime, period: str) -> datetime:
    """Floor one instant to its UTC-aligned bar start for ``period``."""
    _require_aware(instant, "instant")
    step = _duration(period).total_seconds()
    if step <= 0:
        raise ValueError("period must be a positive duration")
    micros = (instant.astimezone(UTC) - _EPOCH) // _ONE_MICROSECOND
    step_micros = _duration(period) // _ONE_MICROSECOND
    return _EPOCH + timedelta(microseconds=(micros // step_micros) * step_micros)


def _validate_window(window_start: datetime, window_end: datetime, period: str) -> None:
    _require_aware(window_start, "window_start")
    _require_aware(window_end, "window_end")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    _duration(period)
    if bar_start_for(window_start, period) != window_start.astimezone(UTC):
        raise ValueError("window_start must be UTC-aligned to the period")
    if bar_start_for(window_end, period) != window_end.astimezone(UTC):
        raise ValueError("window_end must be UTC-aligned to the period")


def effective_window_end(
    window_start: datetime,
    window_end: datetime,
    period: str,
    closed_at: datetime | None,
) -> datetime:
    """Clamp the window to the last fully closed bar at ``closed_at``."""
    _validate_window(window_start, window_end, period)
    if closed_at is None:
        return window_end
    _require_aware(closed_at, "closed_at")
    floored = bar_start_for(closed_at, period)
    return max(window_start, min(window_end, floored))


def expected_bar_starts(
    window_start: datetime, window_end: datetime, period: str
) -> tuple[datetime, ...]:
    """Every aligned slot in the half-open window, including missing ones."""
    _validate_window(window_start, window_end, period)
    duration = _duration(period)
    slots: list[datetime] = []
    slot = window_start.astimezone(UTC)
    while slot < window_end:
        slots.append(slot)
        slot += duration
    return tuple(slots)


def bar_metric_version(period: str) -> MetricVersion:
    """The immutable algorithm/parameter identity for one bar period."""
    _duration(period)
    return build_metric_version(
        algorithm="agg_bars",
        algorithm_version=1,
        parameters={
            "alignment": "UTC_EPOCH_FLOOR",
            "close_rule": "LAST_BY_TRANSACT_TIME_AND_ID",
            "open_rule": "FIRST_BY_TRANSACT_TIME_AND_ID",
            "period": period,
            "quote_volume_rule": "SUM_PRICE_TIMES_QUANTITY",
            "taker_buy_rule": "IS_BUYER_MAKER_FALSE",
            "volume_rule": "SUM_QUANTITY",
        },
    )


def _epoch_millis(value: datetime) -> int:
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def bars_input_hash(
    trades: tuple[AggTrade, ...] | list[AggTrade],
    *,
    symbol: str,
    period: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Hash the canonical row set that produced a bar run."""
    digest = hashlib.sha256()
    digest.update(
        (f"{symbol}|{period}|{utc_timestamp(window_start)}|{utc_timestamp(window_end)}\n").encode()
    )
    ordered = sorted(trades, key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    for trade in ordered:
        digest.update(
            (
                f"{trade.agg_trade_id}|{decimal_str(trade.price)}|"
                f"{decimal_str(trade.quantity)}|{_epoch_millis(trade.transact_time)}|"
                f"{int(trade.is_buyer_maker)}\n"
            ).encode()
        )
    return digest.hexdigest()


@dataclass(frozen=True)
class Bar:
    """One closed OHLCV bar with exact decimal amounts.

    ``quote_volume`` is the exact, unrounded ``sum(price * quantity)`` over the
    bar's trades. It is serialized as a decimal string in the contract, so a
    consumer must compare it as an exact decimal rather than a binary float.
    """

    symbol: str
    period: str
    bar_start: datetime
    bar_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_volume: Decimal
    source_mode: str = "BACKFILL"

    def __post_init__(self) -> None:
        _require_aware(self.bar_start, "bar_start")
        _require_aware(self.bar_end, "bar_end")
        if self.bar_end <= self.bar_start:
            raise ValueError("bar_end must be after bar_start")
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open must lie within [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close must lie within [low, high]")
        for name in ("volume", "quote_volume", "taker_buy_volume"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.trade_count < 0:
            raise ValueError("trade_count must be non-negative")
        if self.source_mode not in SOURCE_MODES:
            raise ValueError(f"unsupported source_mode {self.source_mode!r}")


@dataclass(frozen=True)
class ResampleResult:
    """Bars plus the exact set of slots that had no trades."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    source_mode: str
    bars: tuple[Bar, ...]
    expected_bar_count: int
    missing_bar_starts: tuple[datetime, ...]
    data_quality: DataQuality
    quality_reasons: tuple[str, ...]
    metric_version: str
    input_manifest_hash: str

    def to_contract(self) -> dict[str, object]:
        """Deterministic JSON-ready document used by goldens and the API."""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "window": {
                "start": utc_timestamp(self.window_start),
                "end": utc_timestamp(self.window_end),
            },
            "metric_version": self.metric_version,
            "input_manifest_hash": self.input_manifest_hash,
            "source_mode": self.source_mode,
            "data_quality": self.data_quality.value,
            "quality_reasons": list(self.quality_reasons),
            "expected_bar_count": self.expected_bar_count,
            "missing_bar_count": len(self.missing_bar_starts),
            "bars": [
                {
                    "bar_start": utc_timestamp(bar.bar_start),
                    "bar_end": utc_timestamp(bar.bar_end),
                    "open": decimal_str(bar.open),
                    "high": decimal_str(bar.high),
                    "low": decimal_str(bar.low),
                    "close": decimal_str(bar.close),
                    "volume": decimal_str(bar.volume),
                    "quote_volume": decimal_str(bar.quote_volume),
                    "trade_count": bar.trade_count,
                    "taker_buy_volume": decimal_str(bar.taker_buy_volume),
                }
                for bar in self.bars
            ],
        }


def _quality(
    expected: tuple[datetime, ...], missing: tuple[datetime, ...], bars: tuple[Bar, ...]
) -> tuple[DataQuality, tuple[str, ...]]:
    if not expected:
        return DataQuality.MISSING, ("no bars expected in the requested window",)
    if not bars:
        return DataQuality.MISSING, (f"missing all {len(expected)} expected bars",)
    if missing:
        return (
            DataQuality.PARTIAL,
            (f"missing {len(missing)} of {len(expected)} expected bars",),
        )
    return DataQuality.COMPLETE, ()


def resample_agg_trades(
    trades: tuple[AggTrade, ...] | list[AggTrade],
    *,
    symbol: str,
    period: str,
    window_start: datetime,
    window_end: datetime,
    closed_at: datetime | None = None,
    source_mode: SourceMode = "BACKFILL",
) -> ResampleResult:
    """Aggregate aggTrades into UTC-aligned bars, ignoring unclosed data.

    Duplicate ids, a foreign symbol, or a trade before ``window_start`` are
    rejected. Trades at/after ``window_end`` are rejected as caller errors;
    trades in ``[effective_end, window_end)`` are simply not yet visible and
    are excluded, so a live caller can pass every event it knows.
    """
    if source_mode not in SOURCE_MODES:
        raise ValueError(f"unsupported source_mode {source_mode!r}")
    duration = _duration(period)
    _validate_window(window_start, window_end, period)
    effective_end = effective_window_end(window_start, window_end, period, closed_at)

    seen_ids: set[int] = set()
    selected: list[AggTrade] = []
    for trade in trades:
        if trade.symbol != symbol:
            raise ValueError(f"trade symbol {trade.symbol!r} does not match {symbol!r}")
        if trade.agg_trade_id in seen_ids:
            raise ValueError(f"duplicate agg_trade_id {trade.agg_trade_id}")
        seen_ids.add(trade.agg_trade_id)
        if trade.transact_time < window_start or trade.transact_time >= window_end:
            raise ValueError(f"trade {trade.agg_trade_id} is outside the requested window")
        if trade.transact_time >= effective_end:
            continue
        selected.append(trade)
    selected.sort(key=lambda trade: (trade.transact_time, trade.agg_trade_id))

    buckets: dict[datetime, list[AggTrade]] = {}
    for trade in selected:
        slot = bar_start_for(trade.transact_time, period)
        buckets.setdefault(slot, []).append(trade)

    bars: list[Bar] = []
    for slot in sorted(buckets):
        group = buckets[slot]
        volume = sum((trade.quantity for trade in group), Decimal("0"))
        quote_volume = sum((trade.price * trade.quantity for trade in group), Decimal("0"))
        taker_buy_volume = sum(
            (trade.quantity for trade in group if not trade.is_buyer_maker),
            Decimal("0"),
        )
        prices = [trade.price for trade in group]
        bars.append(
            Bar(
                symbol=symbol,
                period=period,
                bar_start=slot,
                bar_end=slot + duration,
                open=group[0].price,
                high=max(prices),
                low=min(prices),
                close=group[-1].price,
                volume=volume,
                quote_volume=quote_volume,
                trade_count=len(group),
                taker_buy_volume=taker_buy_volume,
                source_mode=source_mode,
            )
        )

    expected: tuple[datetime, ...]
    missing: tuple[datetime, ...]
    if effective_end <= window_start:
        expected = ()
        missing = ()
        quality: DataQuality = DataQuality.MISSING
        reasons: tuple[str, ...] = ("closed_at is before the first fully closed bar",)
    else:
        expected = expected_bar_starts(window_start, effective_end, period)
        present = {bar.bar_start for bar in bars}
        missing = tuple(slot for slot in expected if slot not in present)
        quality, reasons = _quality(expected, missing, tuple(bars))
    return ResampleResult(
        symbol=symbol,
        period=period,
        window_start=window_start,
        window_end=window_end,
        source_mode=source_mode,
        bars=tuple(bars),
        expected_bar_count=len(expected),
        missing_bar_starts=missing,
        data_quality=quality,
        quality_reasons=reasons,
        metric_version=bar_metric_version(period).identifier,
        input_manifest_hash=bars_input_hash(
            selected,
            symbol=symbol,
            period=period,
            window_start=window_start,
            window_end=window_end,
        ),
    )


__all__ = [
    "BAR_PERIODS",
    "SOURCE_MODES",
    "Bar",
    "ResampleResult",
    "SourceMode",
    "bar_metric_version",
    "bar_start_for",
    "bars_input_hash",
    "effective_window_end",
    "expected_bar_starts",
    "resample_agg_trades",
]
