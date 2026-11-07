"""Parquet parts are unique under concurrent writers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.parquet_store import ParquetStore
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.ports.store import StoreUnavailableError


def _trade(agg_trade_id: int = 1, day: int = 5) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=agg_trade_id,
        price=Decimal("100.00"),
        quantity=Decimal("0.5"),
        first_trade_id=agg_trade_id,
        last_trade_id=agg_trade_id,
        transact_time=datetime(2025, 11, day, 0, 0, tzinfo=UTC),
        is_buyer_maker=False,
    )


def test_concurrent_part_writes_get_unique_paths(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = ParquetStore(settings)
    store.ensure_layout()
    trade = _trade()

    def write(_: int) -> str:
        return store.write_agg_trades((trade,), symbol="BTCUSDT", source="test").path

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(write, range(8)))

    assert len(set(paths)) == 8
    assert all(Path(path).is_file() for path in paths)


def _parts(settings: Settings) -> list[Path]:
    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    return sorted(root.glob("date=*/*.parquet"))


def test_unreadable_existing_part_fails_closed_without_writing(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = ParquetStore(settings)
    store.ensure_layout()
    first = store.write_agg_trades((_trade(),), symbol="BTCUSDT", source="test")
    Path(first.path).write_bytes(b"not a parquet file")
    before = _parts(settings)

    with pytest.raises(StoreUnavailableError) as excinfo:
        store.write_agg_trades_batch((_trade(),), symbol="BTCUSDT", source="test")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert _parts(settings) == before


def test_multi_date_publish_failure_removes_already_published_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = ParquetStore(settings)
    store.ensure_layout()
    original = store._write_part

    def flaky(
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        date: str,
        source: str,
    ) -> object:
        if date == "2025-11-06":
            raise OSError("simulated disk full")
        return original(trades, symbol=symbol, date=date, source=source)

    monkeypatch.setattr(store, "_write_part", flaky)

    with pytest.raises(OSError):
        store.write_agg_trades_batch((_trade(1, 5), _trade(2, 6)), symbol="BTCUSDT", source="test")

    assert _parts(settings) == []


def test_concurrent_content_addressed_write_never_overwrites_different_content(
    tmp_path: Path,
) -> None:
    import pyarrow as pa

    from oft.adapters.parquet_store import _publish_table
    from oft.ports.store import StoreUnavailableError

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    directory = settings.data_dir / "metrics" / "orderflow"
    directory.mkdir(parents=True)
    table = pa.table({"value": pa.array([1], pa.int64())})
    existing = directory / "part-prefix-hash.parquet"
    existing.write_bytes(b"different content")

    with pytest.raises(StoreUnavailableError, match="concurrently"):
        _publish_table(table, directory, name_prefix="prefix", stem_hash="hash")
    assert existing.read_bytes() == b"different content"


def test_read_bars_prefers_the_newest_artifact_when_replay_clock_repeats(
    tmp_path: Path,
) -> None:
    """A replay clock can freeze computed_at; run_seq must break the tie."""
    from oft.adapters.parquet_store import read_bars
    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.resampling_service import (
        BarResampleRequest,
        ResamplingService,
    )
    from oft.clock import FixedClock

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    parquet = ParquetStore(settings)
    parquet.ensure_layout()
    clock = FixedClock(datetime(2025, 11, 6, 0, 0, tzinfo=UTC))
    service = ResamplingService(settings=settings, store=store, parquet=parquet, clock=clock)
    request = BarResampleRequest(
        symbol="BTCUSDT",
        period="4h",
        window_start=datetime(2025, 11, 5, 0, 0, tzinfo=UTC),
        window_end=datetime(2025, 11, 5, 4, 0, tzinfo=UTC),
    )
    first = service.resample_and_persist(request, trades=(_trade(1, 5),))
    second_trade = _trade(2, 5).model_copy(update={"price": Decimal("200.00")})
    second = service.resample_and_persist(request, trades=(second_trade,))

    assert first.run.computed_at == second.run.computed_at
    assert first.entry.path != second.entry.path

    rows = read_bars(
        settings,
        symbol="BTCUSDT",
        period="4h",
        metric_version=second.run.result.metric_version,
        store=store,
    )

    assert [row["close"] for row in rows] == ["200.00"]
    assert rows[0]["run_id"] == second.run.run_id
