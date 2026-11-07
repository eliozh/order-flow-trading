"""Application service: resample events into versioned, immutable bar parts.

The service is the boundary between the pure resampler and persistence. It
loads only manifest-registered aggTrades parts, injects time through
``ClockPort``, carries missing-bar gaps into the downstream metric input, and
handles late events by recomputing only the trailing N bars as a new versioned
run. An existing published part is never edited in place.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from oft.config import Settings
from oft.domain.data_quality import DataGap, DataQuality, GapSeverity
from oft.domain.events import AggTrade
from oft.domain.metrics.delta import parse_period
from oft.domain.resampling import (
    SOURCE_MODES,
    Bar,
    ResampleResult,
    SourceMode,
    bar_start_for,
    effective_window_end,
    expected_bar_starts,
    resample_agg_trades,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import ManifestEntry, StorePort, StoreUnavailableError

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
DEFAULT_LATE_RECOMPUTE_BARS = 2


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


@dataclass(frozen=True)
class BarResampleRequest:
    """Inputs that fully determine one resampled bar run."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    source_mode: SourceMode = "BACKFILL"

    def __post_init__(self) -> None:
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must match ^[A-Z0-9]{2,24}$")
        parse_period(self.period)
        _require_aware(self.window_start, "window_start")
        _require_aware(self.window_end, "window_end")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if bar_start_for(self.window_start, self.period) != self.window_start.astimezone(UTC):
            raise ValueError("window_start must be UTC-aligned to the period")
        if bar_start_for(self.window_end, self.period) != self.window_end.astimezone(UTC):
            raise ValueError("window_end must be UTC-aligned to the period")
        if self.source_mode not in SOURCE_MODES:
            raise ValueError(f"unsupported source_mode {self.source_mode!r}")


@dataclass(frozen=True)
class BarMetricInput:
    """The bar collection and explicit gaps handed to a metric consumer."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    bars: tuple[Bar, ...]
    gaps: tuple[DataGap, ...]
    data_quality: DataQuality
    quality_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResampleRun:
    """A resampled result plus provenance and its versioned run identity."""

    result: ResampleResult
    source_digest: str
    computed_at: str
    run_id: str
    last_n: int | None = None
    recomputed_from: datetime | None = None

    def bar_metric_input(self) -> BarMetricInput:
        """Expose bars and contiguous gaps; missing bars are never invented."""
        gaps = _missing_bar_gaps(self.result, detected_at=self.computed_at)
        return BarMetricInput(
            symbol=self.result.symbol,
            period=self.result.period,
            window_start=self.result.window_start,
            window_end=self.result.window_end,
            bars=self.result.bars,
            gaps=gaps,
            data_quality=self.result.data_quality,
            quality_reasons=self.result.quality_reasons,
        )


@dataclass(frozen=True)
class PersistedResample:
    """A resample run and the immutable manifest row that stores it."""

    run: ResampleRun
    entry: ManifestEntry


class BarWriterPort(Protocol):
    """Publishes immutable bar Parquet parts."""

    def write_bar_part(
        self,
        bars: Sequence[Bar],
        *,
        symbol: str,
        period: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        source_mode: str,
        source_digest: str,
        run_id: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry: ...


def _missing_bar_gaps(result: ResampleResult, *, detected_at: str) -> tuple[DataGap, ...]:
    duration = parse_period(result.period)
    if not result.missing_bar_starts:
        return ()
    ranges: list[tuple[datetime, datetime]] = []
    range_start = result.missing_bar_starts[0]
    previous = range_start
    for slot in result.missing_bar_starts[1:]:
        if slot != previous + duration:
            ranges.append((range_start, previous + duration))
            range_start = slot
        previous = slot
    ranges.append((range_start, previous + duration))
    return tuple(
        DataGap(
            gap_id=(
                f"GAP-{result.symbol}-bars-{result.period}-{start.astimezone(UTC):%Y%m%dT%H%M%S}"
            ),
            symbol=result.symbol,
            data_type="bars",
            start=utc_timestamp(start),
            end=utc_timestamp(end),
            reason="MISSING_BARS",
            severity=GapSeverity.WARNING,
            detected_at=detected_at,
            source="RESAMPLE",
        )
        for start, end in ranges
    )


def _window_days(window_start: datetime, window_end: datetime) -> tuple[str, ...]:
    first = window_start.astimezone(UTC).date()
    last = (window_end - timedelta(microseconds=1)).astimezone(UTC).date()
    return tuple(
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    )


def load_published_agg_trades(
    settings: Settings,
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    store: StorePort | None = None,
) -> tuple[AggTrade, ...]:
    """Read manifest-registered aggTrades parts intersecting the window."""
    root = settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"
    allowed: set[str] | None = None
    if store is not None:
        allowed = {entry.path for entry in store.list_manifest() if entry.data_type == "agg_trades"}
    parts: list[str] = []
    for day in _window_days(window_start, window_end):
        directory = root / f"date={day}"
        if not directory.is_dir():
            continue
        parts.extend(
            str(path)
            for path in sorted(directory.glob("*.parquet"))
            if allowed is None or str(path) in allowed
        )
    if not parts:
        return ()
    import pyarrow as pa
    import pyarrow.dataset as dataset

    try:
        table = dataset.dataset(parts, format="parquet").to_table(
            columns=[
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "transact_time",
                "is_buyer_maker",
                "symbol",
            ]
        )
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(f"cannot read agg_trades parts for {symbol}: {exc}") from exc

    trades: list[AggTrade] = []
    for row in table.to_pylist():
        transact_time = row["transact_time"]
        if transact_time.tzinfo is None:
            transact_time = transact_time.replace(tzinfo=UTC)
        if not (window_start <= transact_time < window_end):
            continue
        trades.append(
            AggTrade(
                symbol=row["symbol"],
                agg_trade_id=row["agg_trade_id"],
                price=Decimal(str(row["price"])),
                quantity=Decimal(str(row["quantity"])),
                first_trade_id=row["first_trade_id"],
                last_trade_id=row["last_trade_id"],
                transact_time=transact_time,
                is_buyer_maker=row["is_buyer_maker"],
            )
        )
    trades.sort(key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    return tuple(trades)


def _merge_trades(base: Sequence[AggTrade], late: Sequence[AggTrade]) -> tuple[AggTrade, ...]:
    by_id: dict[int, AggTrade] = {trade.agg_trade_id: trade for trade in base}
    for trade in late:
        by_id[trade.agg_trade_id] = trade
    return tuple(
        sorted(by_id.values(), key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    )


class ResamplingService:
    """Compute, persist, and late-recompute deterministic UTC-aligned bars."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StorePort,
        parquet: BarWriterPort,
        clock: ClockPort,
        late_recompute_bars: int = DEFAULT_LATE_RECOMPUTE_BARS,
    ) -> None:
        if late_recompute_bars < 1:
            raise ValueError("late_recompute_bars must be >= 1")
        self._settings = settings
        self._store = store
        self._parquet = parquet
        self._clock = clock
        self._late_recompute_bars = late_recompute_bars

    def _source_digest(self, request: BarResampleRequest, *, fallback: str) -> str:
        days = set(_window_days(request.window_start, request.window_end))
        entries = sorted(
            (entry.path, entry.checksum)
            for entry in self._store.list_manifest()
            if entry.data_type == "agg_trades"
            and entry.symbol == request.symbol
            and entry.date in days
        )
        if not entries:
            return fallback
        payload = "\n".join(f"{path}|{checksum}" for path, checksum in entries)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def compute(
        self,
        request: BarResampleRequest,
        *,
        trades: Sequence[AggTrade] | None = None,
        closed_at: datetime | None = None,
    ) -> ResampleRun:
        source = (
            tuple(trades)
            if trades is not None
            else load_published_agg_trades(
                self._settings,
                symbol=request.symbol,
                window_start=request.window_start,
                window_end=request.window_end,
                store=self._store,
            )
        )
        result = resample_agg_trades(
            source,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            closed_at=closed_at if closed_at is not None else self._clock.now(),
            source_mode=request.source_mode,
        )
        return self._run(
            result,
            source_digest=self._source_digest(request, fallback=result.input_manifest_hash),
        )

    def _run(
        self,
        result: ResampleResult,
        *,
        source_digest: str,
        last_n: int | None = None,
        recomputed_from: datetime | None = None,
    ) -> ResampleRun:
        computed_at = utc_timestamp(self._clock.now())
        run_id = f"{result.metric_version}-{result.input_manifest_hash[:16]}"
        return ResampleRun(
            result=result,
            source_digest=source_digest,
            computed_at=computed_at,
            run_id=run_id,
            last_n=last_n,
            recomputed_from=recomputed_from,
        )

    def persist(self, run: ResampleRun) -> ManifestEntry:
        """Publish the run's bars as one immutable content-addressed part."""
        entry = self._parquet.write_bar_part(
            run.result.bars,
            symbol=run.result.symbol,
            period=run.result.period,
            window_start=run.result.window_start,
            window_end=run.result.window_end,
            metric_version=run.result.metric_version,
            input_manifest_hash=run.result.input_manifest_hash,
            source_mode=run.result.source_mode,
            source_digest=run.source_digest,
            run_id=run.run_id,
            computed_at=run.computed_at,
            data_quality=run.result.data_quality.value,
            quality_reasons=run.result.quality_reasons,
        )
        try:
            self._store.upsert_manifest_entry(entry)
        except Exception:
            try:
                referenced = self._store.get_manifest_entry(entry.path) is not None
            except StoreUnavailableError:
                referenced = True
            if not referenced:
                Path(entry.path).unlink(missing_ok=True)
            raise
        return entry

    def resample_and_persist(
        self,
        request: BarResampleRequest,
        *,
        trades: Sequence[AggTrade] | None = None,
        closed_at: datetime | None = None,
    ) -> PersistedResample:
        run = self.compute(request, trades=trades, closed_at=closed_at)
        return PersistedResample(run=run, entry=self.persist(run))

    def recompute_late(
        self,
        request: BarResampleRequest,
        *,
        late_trades: Sequence[AggTrade],
        trades: Sequence[AggTrade] | None = None,
        last_n: int | None = None,
    ) -> ResampleRun:
        """Recompute only the trailing N closed bars as a new versioned run.

        The late event must fall inside that trailing window; a much older
        event is rejected rather than silently rewriting a long-published
        range. Replaying the same event is idempotent because the run id and
        part identity are content-addressed.
        """
        n = self._late_recompute_bars if last_n is None else last_n
        if n < 1:
            raise ValueError("last_n must be >= 1")
        if not late_trades:
            raise ValueError("late_trades must not be empty")
        source = (
            tuple(trades)
            if trades is not None
            else load_published_agg_trades(
                self._settings,
                symbol=request.symbol,
                window_start=request.window_start,
                window_end=request.window_end,
                store=self._store,
            )
        )
        for trade in late_trades:
            if trade.symbol != request.symbol:
                raise ValueError(
                    f"late trade symbol {trade.symbol!r} does not match {request.symbol!r}"
                )
            if not (request.window_start <= trade.transact_time < request.window_end):
                raise ValueError("late trade is outside the requested window")
        closed = self._clock.now()
        effective_end = effective_window_end(
            request.window_start, request.window_end, request.period, closed
        )
        if effective_end <= request.window_start:
            result = resample_agg_trades(
                tuple(late_trades),
                symbol=request.symbol,
                period=request.period,
                window_start=request.window_start,
                window_end=request.window_end,
                closed_at=closed,
                source_mode=request.source_mode,
            )
            return self._run(
                result,
                source_digest=self._source_digest(request, fallback=result.input_manifest_hash),
                last_n=n,
            )
        slots = expected_bar_starts(request.window_start, effective_end, request.period)
        tail_start = slots[max(0, len(slots) - n)]
        for trade in late_trades:
            if bar_start_for(trade.transact_time, request.period) < tail_start:
                raise ValueError(f"late event is older than the last {n} bars")
        merged = _merge_trades(source, late_trades)
        tail_trades = [trade for trade in merged if trade.transact_time >= tail_start]
        result = resample_agg_trades(
            tail_trades,
            symbol=request.symbol,
            period=request.period,
            window_start=tail_start,
            window_end=request.window_end,
            closed_at=closed,
            source_mode=request.source_mode,
        )
        return self._run(
            result,
            source_digest=self._source_digest(request, fallback=result.input_manifest_hash),
            last_n=n,
            recomputed_from=tail_start,
        )


__all__ = [
    "DEFAULT_LATE_RECOMPUTE_BARS",
    "BarMetricInput",
    "BarResampleRequest",
    "BarWriterPort",
    "PersistedResample",
    "ResampleRun",
    "ResamplingService",
    "load_published_agg_trades",
]
