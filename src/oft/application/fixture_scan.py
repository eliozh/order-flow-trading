"""Fixture-backed scan: source -> Parquet + manifest -> delta -> SQLite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from oft.adapters.fixture_market_data import FixtureDataError
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.metrics.delta import compute_delta, parse_period
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import ManifestEntry, ScanRecord, StorePort


class ParquetWriterPort(Protocol):
    def write_agg_trades(
        self,
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        source: str,
    ) -> ManifestEntry: ...


class ScanContainer(Protocol):
    @property
    def settings(self) -> Settings: ...

    @property
    def clock(self) -> ClockPort: ...

    @property
    def market_data(self) -> MarketDataPort: ...

    @property
    def store(self) -> StorePort: ...

    @property
    def parquet(self) -> ParquetWriterPort: ...


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


def _window_bounds(trades: tuple, period: str) -> tuple[datetime, datetime]:
    duration = parse_period(period)
    seconds = int(duration.total_seconds())
    first = min(trade.transact_time for trade in trades)
    last = max(trade.transact_time for trade in trades)
    start_epoch = int(first.timestamp()) // seconds * seconds
    end_epoch = (int(last.timestamp()) // seconds + 1) * seconds
    return (
        datetime.fromtimestamp(start_epoch, tz=UTC),
        datetime.fromtimestamp(end_epoch, tz=UTC),
    )


def run_fixture_scan(
    container: ScanContainer,
    *,
    fixture_path: Path | str,
    symbol: str,
    period: str,
    metrics_only: bool = True,
    trigger_source: str = "MANUAL",
) -> ScanRecord:
    """Run one deterministic fixture scan through every layer."""
    clock = container.clock
    started = clock.now()

    trades = container.market_data.load_agg_trades(fixture_path, symbol)
    window_start, window_end = _window_bounds(trades, period)
    try:
        delta = compute_delta(
            trades,
            symbol=symbol,
            period=period,
            window_start=window_start,
            window_end=window_end,
        )
    except ValueError as exc:
        raise FixtureDataError(f"cannot compute delta: {exc}") from exc

    source = f"fixture:{Path(fixture_path).name}"
    manifest = container.parquet.write_agg_trades(trades, symbol=symbol, source=source)
    container.store.record_manifest(manifest)

    finished = clock.now()
    created_at = utc_timestamp(finished)
    record = ScanRecord(
        scan_id=f"SCAN-{finished:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
        symbol=symbol,
        period=period,
        status="DONE",
        trigger_source=trigger_source,
        metrics_only=metrics_only,
        fixture_path=str(fixture_path),
        started_at=utc_timestamp(started),
        finished_at=created_at,
        window_start=utc_timestamp(window_start),
        window_end=utc_timestamp(window_end),
        trade_count=delta.trade_count,
        buy_volume=_decimal_str(delta.buy_volume),
        sell_volume=_decimal_str(delta.sell_volume),
        delta=_decimal_str(delta.delta),
        cvd=_decimal_str(delta.cvd),
        data_quality="COMPLETE",
        manifest_path=manifest.path,
        parquet_rows=manifest.rows,
        created_at=created_at,
    )
    container.store.save_scan(record)
    return record


def get_scan(container: ScanContainer, scan_id: str) -> ScanRecord | None:
    return container.store.get_scan(scan_id)


def latest_scan(container: ScanContainer) -> ScanRecord | None:
    return container.store.latest_scan()
