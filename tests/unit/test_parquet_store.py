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
