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
from oft.ports.store import ManifestEntry, StoreUnavailableError

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


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        return self._write_part(trades, symbol=symbol, date=date, source=source)

    def _existing_agg_trade_ids(self, symbol: str, dates: set[str]) -> set[int]:
        """Ids already published for this symbol, limited to the batch date range.

        A read failure must fail closed: silently treating an unreadable part as
        empty would disable dedupe and duplicate every row on a re-run. Empty or
        non-Parquet directories are not read failures.
        """
        root = self._settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"
        if not root.is_dir():
            return set()
        import pyarrow.dataset as dataset

        parts: list[str] = []
        for day in sorted(dates):
            directory = root / f"date={day}"
            if not directory.is_dir():
                continue
            parts.extend(str(path) for path in directory.glob("*.parquet") if path.is_file())
        if not parts:
            return set()
        try:
            table = dataset.dataset(parts, format="parquet").to_table(columns=["agg_trade_id"])
        except (OSError, ValueError) as exc:
            raise StoreUnavailableError(
                f"cannot read existing agg_trade parts for {symbol}: {exc}"
            ) from exc
        return {
            int(value) for value in table.column("agg_trade_id").to_pylist() if value is not None
        }

    def write_agg_trades_batch(
        self,
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        source: str,
    ) -> tuple[ManifestEntry, ...]:
        """Dedupe by ``agg_trade_id`` and publish one part per UTC date.

        Deduplication is scoped to the batch's date partitions, so re-running
        the same archive range cannot duplicate rows without scanning the whole
        symbol history.
        """
        if not trades:
            return ()
        batch_dates = {trade.transact_time.astimezone(UTC).date().isoformat() for trade in trades}
        seen = self._existing_agg_trade_ids(symbol, batch_dates)
        unique: list[AggTrade] = []
        for trade in trades:
            if trade.agg_trade_id in seen:
                continue
            seen.add(trade.agg_trade_id)
            unique.append(trade)
        if not unique:
            return ()
        groups: dict[str, list[AggTrade]] = {}
        for trade in unique:
            day = trade.transact_time.astimezone(UTC).date().isoformat()
            groups.setdefault(day, []).append(trade)
        published: list[ManifestEntry] = []
        try:
            for day in sorted(groups):
                published.append(
                    self._write_part(groups[day], symbol=symbol, date=day, source=source)
                )
        except Exception:
            # Never leave a partially published multi-date batch behind.
            for entry in published:
                path = Path(entry.path)
                path.unlink(missing_ok=True)
                _fsync_dir(path.parent)
            raise
        return tuple(published)

    def _write_part(
        self,
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        date: str,
        source: str,
    ) -> ManifestEntry:
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
        ingested_at_dt = datetime.now(UTC)

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
                    [ingested_at_dt for _ in trades],
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
            _fsync_dir(directory)
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
