"""Backpressure: bounded queue, WAL spill, flush thresholds, orphan quarantine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.parquet_store import (
    FlushPolicy,
    LiveParquetBuffer,
    quarantine_orphan_live_parts,
)
from oft.adapters.sqlite_store import SqliteStore
from oft.application.ingestion_supervisor import (
    BoundedEventQueue,
    EnqueueOutcome,
    IngestionSupervisor,
    WalFullError,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.events import AggTrade

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ws"


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


def _trade(agg_trade_id: int) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=agg_trade_id,
        price=Decimal("35000.00"),
        quantity=Decimal("0.100"),
        first_trade_id=agg_trade_id,
        last_trade_id=agg_trade_id,
        transact_time=NOW,
        is_buyer_maker=False,
    )


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    return settings


def _buffer(settings: Settings, clock: MutableClock, **policy: int) -> LiveParquetBuffer:
    store = SqliteStore(settings)
    return LiveParquetBuffer(
        settings=settings,
        store=store,
        clock=clock,
        policy=FlushPolicy(**policy),
    )


def test_supervisor_flushes_after_the_interval_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    store = SqliteStore(settings)
    buffer = _buffer(settings, clock, max_rows=50_000, max_interval_seconds=30)
    queue = BoundedEventQueue(
        maxsize=100,
        wal_path=settings.data_dir / "run" / "ingest.wal",
        clock=clock,
    )

    class TimedSource:
        def __iter__(self):
            yield (
                '{"e":"aggTrade","E":1793966400000,"s":"BTCUSDT","a":1,'
                '"p":"35000.0","q":"0.1","f":1,"l":1,"T":1793966400000,"m":false}'
            )
            clock.advance(31)
            yield (
                '{"e":"aggTrade","E":1793966401000,"s":"BTCUSDT","a":2,'
                '"p":"35001.0","q":"0.1","f":2,"l":2,"T":1793966401000,"m":false}'
            )

    supervisor = IngestionSupervisor(
        settings=settings,
        source=TimedSource(),
        store=store,
        parquet=buffer,
        clock=clock,
        queue=queue,
        sleep=lambda _seconds: None,
    )

    stats = supervisor.run()

    assert stats.persisted_rows == 2
    assert stats.flushes >= 1
    import pyarrow.dataset as dataset

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    table = dataset.dataset([str(path) for path in root.glob("date=*/*.parquet")]).to_table()
    assert table.num_rows == 2


def test_bounded_queue_spills_to_wal_and_exports_counters(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        maxsize=1,
        wal_path=tmp_path / "ingest.wal",
        clock=FixedClock(NOW),
    )

    assert queue.put(_trade(1)) is EnqueueOutcome.QUEUED
    assert queue.put(_trade(2)) is EnqueueOutcome.SPILLED

    assert queue.stats["queued"] == 1
    assert queue.stats["spilled"] == 1
    assert queue.stats["dropped"] == 0
    assert queue.stats["wal_bytes"] > 0
    assert [event.agg_trade_id for event in queue.replay_wal()] == [2]


def test_wal_full_raises_instead_of_silently_dropping(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        maxsize=1,
        wal_path=tmp_path / "ingest.wal",
        max_wal_bytes=8,
        clock=FixedClock(NOW),
    )
    queue.put(_trade(1))

    with pytest.raises(WalFullError):
        queue.put(_trade(2))

    assert queue.stats["blocked"] == 1
    assert queue.stats["dropped"] == 0


def test_supervisor_spills_and_persists_every_event_without_loss(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    store = SqliteStore(settings)
    buffer = _buffer(settings, clock, max_rows=50_000, max_interval_seconds=30)
    queue = BoundedEventQueue(
        maxsize=1,
        wal_path=settings.data_dir / "run" / "ingest.wal",
        clock=clock,
    )
    source = [
        '{"e":"aggTrade","E":1793966400000,"s":"BTCUSDT","a":1,"p":"35000.0",'
        '"q":"0.1","f":1,"l":1,"T":1793966400000,"m":false}',
        '{"e":"aggTrade","E":1793966400100,"s":"BTCUSDT","a":2,"p":"35001.0",'
        '"q":"0.1","f":2,"l":2,"T":1793966400100,"m":false}',
        '{"e":"aggTrade","E":1793966400200,"s":"BTCUSDT","a":3,"p":"35002.0",'
        '"q":"0.1","f":3,"l":3,"T":1793966400200,"m":false}',
    ]
    supervisor = IngestionSupervisor(
        settings=settings,
        source=source,
        store=store,
        parquet=buffer,
        clock=clock,
        queue=queue,
        sleep=lambda _seconds: None,
    )

    stats = supervisor.run()

    assert stats.accepted == 3
    assert stats.persisted_rows == 3
    assert stats.spilled >= 1
    assert stats.dropped == 0


def test_flush_at_row_threshold_writes_one_manifest_row(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    buffer = _buffer(settings, clock, max_rows=2, max_interval_seconds=30)

    buffer.add(_trade(1))
    buffer.add(_trade(2))
    assert buffer.should_flush() is True
    entries = buffer.maybe_flush()

    assert len(entries) == 1
    assert entries[0].data_type == "agg_trades"
    assert entries[0].rows == 2
    assert Path(entries[0].path).is_file()
    assert not list(Path(entries[0].path).parent.glob("*.tmp"))
    store = SqliteStore(settings)
    assert store.get_manifest_entry(entries[0].path) is not None


def test_flush_after_thirty_seconds_with_pending_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    buffer = _buffer(settings, clock, max_rows=50_000, max_interval_seconds=30)
    buffer.add(_trade(1))

    assert buffer.should_flush() is False
    clock.advance(31)
    assert buffer.should_flush() is True
    entries = buffer.maybe_flush()

    assert len(entries) == 1
    assert entries[0].rows == 1


def test_second_flush_does_not_duplicate_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    buffer = _buffer(settings, clock, max_rows=1, max_interval_seconds=30)
    buffer.add(_trade(1))
    first = buffer.flush()
    buffer.add(_trade(1))
    second = buffer.flush()

    assert len(first) == 1
    assert second == ()
    import pyarrow.dataset as dataset

    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    table = dataset.dataset([str(path) for path in root.glob("date=*/*.parquet")]).to_table()
    assert table.num_rows == 1


def test_orphan_part_is_quarantined_on_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock(NOW)
    store = SqliteStore(settings)
    buffer = _buffer(settings, clock, max_rows=1, max_interval_seconds=30)
    buffer.add(_trade(1))
    entry = buffer.flush()[0]
    store.delete_manifest_entry(entry.path)

    quarantined = quarantine_orphan_live_parts(settings, store, clock=clock)

    assert [path.name for path in quarantined] == [Path(entry.path).name]
    assert not Path(entry.path).exists()
    assert Path(quarantined[0]).is_file()
    assert list((settings.data_dir / "quarantine").rglob("*.parquet"))
