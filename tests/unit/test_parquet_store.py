"""Parquet parts are unique under concurrent writers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from oft.adapters.parquet_store import ParquetStore
from oft.config import Settings
from oft.domain.events import AggTrade


def _trade() -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=1,
        price=Decimal("100.00"),
        quantity=Decimal("0.5"),
        first_trade_id=1,
        last_trade_id=1,
        transact_time=datetime(2025, 11, 5, 0, 0, tzinfo=UTC),
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
