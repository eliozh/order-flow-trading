"""Immutable Parquet parts for normalized market events."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.time import utc_timestamp
from oft.ports.store import ManifestEntry

if TYPE_CHECKING:
    import duckdb

_LAYOUT = ("market", "metrics", "footprint", "liquidations")


def ensure_layout(settings: Settings) -> tuple[Path, ...]:
    """Create the immutable-part directories under the data directory."""
    roots = tuple(settings.data_dir / name for name in _LAYOUT)
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


def open_duckdb_catalog(settings: Settings) -> duckdb.DuckDBPyConnection:
    """Open the local DuckDB catalog; views are added by later slices."""
    import duckdb

    catalog_dir = settings.data_dir / "duckdb"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(catalog_dir / "oft.duckdb"))


def _record_hash(trade: AggTrade) -> str:
    payload = "|".join(
        (
            trade.symbol,
            str(trade.agg_trade_id),
            str(trade.price),
            str(trade.quantity),
            trade.transact_time.isoformat(),
            "1" if trade.is_buyer_maker else "0",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ParquetStore:
    """Writes one immutable part per fixture import and returns its manifest row."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def ensure_layout(self) -> tuple[Path, ...]:
        return ensure_layout(self._settings)

    def write_agg_trades(
        self,
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        source: str,
    ) -> ManifestEntry:
        if not trades:
            raise ValueError("cannot write an empty agg_trades part")

        first = trades[0]
        date = first.transact_time.astimezone(UTC).date().isoformat()
        directory = (
            self._settings.data_dir
            / "market"
            / "um"
            / "agg_trades"
            / f"symbol={symbol}"
            / f"date={date}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        # UUID names remove the check-then-create race between concurrent scans.
        final_path = directory / f"part-{uuid.uuid4().hex}.parquet"
        temp_path = directory / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
        ingested_at = utc_timestamp(datetime.now(UTC))

        table = pa.table(
            {
                "agg_trade_id": pa.array([t.agg_trade_id for t in trades], pa.int64()),
                "price": pa.array([str(t.price) for t in trades], pa.string()),
                "quantity": pa.array([str(t.quantity) for t in trades], pa.string()),
                "first_trade_id": pa.array([t.first_trade_id for t in trades], pa.int64()),
                "last_trade_id": pa.array([t.last_trade_id for t in trades], pa.int64()),
                "transact_time": pa.array(
                    [t.transact_time.astimezone(UTC) for t in trades],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "is_buyer_maker": pa.array([t.is_buyer_maker for t in trades], pa.bool_()),
                "symbol": pa.array([t.symbol for t in trades], pa.string()),
                "source": pa.array([source for _ in trades], pa.string()),
                "ingested_at": pa.array(
                    [datetime.now(UTC) for _ in trades],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "record_hash": pa.array([_record_hash(t) for t in trades], pa.string()),
            }
        )

        try:
            pq.write_table(table, temp_path, compression="snappy")
            with temp_path.open("rb") as handle:
                os.fsync(handle.fileno())
            checksum = hashlib.sha256(temp_path.read_bytes()).hexdigest()
            os.replace(temp_path, final_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        size = final_path.stat().st_size
        return ManifestEntry(
            path=str(final_path),
            data_type="agg_trades",
            symbol=symbol,
            date=date,
            rows=len(trades),
            bytes=size,
            checksum=checksum,
            source=source,
            created_at=ingested_at,
        )
