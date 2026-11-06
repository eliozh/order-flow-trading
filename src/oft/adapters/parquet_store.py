"""Immutable Parquet parts for normalized market events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.metrics.types import MetricRow, decimal_str
from oft.domain.time import utc_timestamp
from oft.ports.store import ManifestEntry, StorePort, StoreUnavailableError

if TYPE_CHECKING:
    import duckdb

_LAYOUT = ("market", "metrics", "footprint", "liquidations")
METRICS_DATA_TYPE = "metrics"


def ensure_layout(settings: Settings) -> tuple[Path, ...]:
    """Create the immutable-part directories under the data directory."""
    roots = tuple(settings.data_dir / name for name in _LAYOUT)
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


_VERSION_TOKEN = re.compile(r"^[a-z][a-z0-9_]*\.v[0-9]+\.[0-9a-f]{16}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")


def _validate_metric_path(symbol: str, period: str) -> None:
    if _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("symbol contains unsafe path characters")
    from oft.domain.metrics.delta import parse_period

    parse_period(period)


def metrics_root(settings: Settings, *, symbol: str, period: str, metric_version: str) -> Path:
    """Return the versioned directory for one symbol/period metric version."""
    _validate_metric_path(symbol, period)
    if _VERSION_TOKEN.fullmatch(metric_version) is None:
        raise ValueError("metric_version contains unsafe path characters")
    return (
        settings.data_dir
        / "metrics"
        / "orderflow"
        / f"symbol={symbol}"
        / f"period={period}"
        / f"metric_version={metric_version}"
    )


def metrics_parts(
    settings: Settings,
    *,
    symbol: str,
    period: str,
    metric_version: str | None = None,
) -> tuple[Path, ...]:
    """List published metrics parts, optionally filtered to one version."""
    _validate_metric_path(symbol, period)
    root = settings.data_dir / "metrics" / "orderflow" / f"symbol={symbol}" / f"period={period}"
    if not root.is_dir():
        return ()
    if metric_version is None:
        pattern = "metric_version=*/date=*/*.parquet"
    else:
        if _VERSION_TOKEN.fullmatch(metric_version) is None:
            raise ValueError("metric_version contains unsafe path characters")
        pattern = f"metric_version={metric_version}/date=*/*.parquet"
    return tuple(sorted(root.glob(pattern)))


def read_metrics(
    settings: Settings,
    *,
    symbol: str,
    period: str,
    metric_version: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    store: StorePort | None = None,
) -> tuple[dict[str, object], ...]:
    """Read every row for one metric version in deterministic order.

    Only manifest-registered metrics parts participate; an unregistered
    (orphaned) part can never shadow a published run. Optional window bounds
    are pushed down into the Arrow dataset scan.
    """
    parts = metrics_parts(settings, symbol=symbol, period=period, metric_version=metric_version)
    if store is None:
        from oft.adapters.sqlite_store import SqliteStore

        store = SqliteStore(settings)
    store_path = getattr(store, "path", None)
    if store_path is not None and not Path(store_path).exists():
        raise StoreUnavailableError("manifest database is unreadable")
    registered = {
        entry.path
        for entry in store.list_manifest()
        if entry.data_type == METRICS_DATA_TYPE and entry.status in {"PUBLISHED", "COMPLETE"}
    }
    parts = tuple(path for path in parts if str(path) in registered)
    if not parts:
        return ()
    import pyarrow.dataset as dataset

    filters = []
    if window_start is not None:
        filters.append(
            dataset.field("window_start")
            >= pa.scalar(window_start.astimezone(UTC), type=pa.timestamp("ms", tz="UTC"))
        )
    if window_end is not None:
        filters.append(
            dataset.field("window_end")
            <= pa.scalar(window_end.astimezone(UTC), type=pa.timestamp("ms", tz="UTC"))
        )
    filter_expr = None
    if filters:
        filter_expr = filters[0]
        for extra in filters[1:]:
            filter_expr = filter_expr & extra

    def content_digest(rows: list[dict[str, object]]) -> str:
        payload = [
            {key: value for key, value in sorted(row.items()) if key != "computed_at"}
            for row in rows
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    candidates: dict[tuple[object, ...], list[dict[str, object]]] = {}
    try:
        for path in parts:
            table = dataset.dataset([str(path)], format="parquet").to_table(filter=filter_expr)
            part_rows = cast("list[dict[str, object]]", table.to_pylist())
            if not part_rows:
                continue
            document_key = (
                part_rows[0].get("metric_version"),
                part_rows[0].get("symbol"),
                part_rows[0].get("period"),
                part_rows[0].get("window_start"),
                part_rows[0].get("window_end"),
            )
            quality = next(
                (
                    str(item.get("data_quality"))
                    for item in part_rows
                    if item.get("data_quality") is not None
                ),
                "",
            )
            candidates.setdefault(document_key, []).append(
                {
                    "path": str(path),
                    "rows": part_rows,
                    "rank": {"COMPLETE": 2, "PARTIAL": 1, "MISSING": 0}.get(quality, 0),
                    "computed": max(str(item.get("computed_at") or "") for item in part_rows),
                    "hash": content_digest(part_rows),
                }
            )
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(f"cannot read metrics parts for {symbol}: {exc}") from exc

    # One part per logical document. Only an exact tie on quality + computed_at
    # with different content is ambiguous and therefore an explicit error.
    selected: list[dict[str, object]] = []
    for doc_key, options in candidates.items():
        best = max(
            options,
            key=lambda option: (
                cast("int", option["rank"]),
                str(option["computed"]),
                str(option["hash"]),
            ),
        )
        for other in options:
            if other is best:
                continue
            if (
                other["rank"] == best["rank"]
                and other["computed"] == best["computed"]
                and other["hash"] != best["hash"]
            ):
                raise ValueError(
                    f"conflicting metrics parts for {doc_key}: {best['path']} vs {other['path']}"
                )
        discarded = [str(option["path"]) for option in options if option is not best]
        if discarded:
            structlog.get_logger(__name__).warning(
                "metrics_run_selected",
                selected_run=best["path"],
                discarded_runs=discarded,
                metric_version=doc_key[0],
                window_start=str(doc_key[3]),
                window_end=str(doc_key[4]),
            )
        selected.extend(cast("list[dict[str, object]]", best["rows"]))
    rows = selected

    def _price_key(value: object) -> Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal("-Infinity")

    rows.sort(
        key=lambda row: (
            str(row.get("window_start")),
            str(row.get("window_end")),
            str(row.get("record_type")),
            str(row.get("bar_start")),
            _price_key(row.get("price_low")),
        )
    )
    unique: list[dict[str, object]] = []
    seen: dict[tuple[object, ...], dict[str, object]] = {}
    duplicate_count = 0

    def comparable(row: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in row.items() if key != "computed_at"}

    for row in rows:
        key = (
            row.get("record_type"),
            row.get("metric_version"),
            row.get("symbol"),
            row.get("window_start"),
            row.get("window_end"),
            row.get("bar_start"),
            row.get("price_low"),
        )
        if key in seen:
            if comparable(seen[key]) != comparable(row):
                raise ValueError(f"conflicting metrics rows for {key} in version {metric_version}")
            duplicate_count += 1
            continue
        seen[key] = row
        unique.append(row)
    if duplicate_count:
        structlog.get_logger(__name__).warning(
            "metrics_duplicate_logical_row",
            symbol=symbol,
            period=period,
            metric_version=metric_version,
            duplicates=duplicate_count,
        )
    return tuple(unique)


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


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else decimal_str(value)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _publish_table(
    table: pa.Table,
    directory: Path,
    *,
    name_prefix: str,
    stem_hash: str,
) -> tuple[Path, str, int]:
    """Write a Parquet table via temp -> fsync -> content-addressed rename."""
    temp_path = directory / f".part-{uuid.uuid4().hex}.tmp"
    final_path = directory / f"part-{name_prefix}-{stem_hash[:16]}.parquet"
    checksum = ""
    try:
        pq.write_table(table, temp_path, compression="snappy")
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        checksum = hashlib.sha256(temp_path.read_bytes()).hexdigest()
        try:
            os.link(temp_path, final_path)
        except FileExistsError:
            existing = hashlib.sha256(final_path.read_bytes()).hexdigest()
            if existing != checksum:
                raise StoreUnavailableError(
                    f"metrics part changed concurrently: {final_path.name}"
                ) from None
        _fsync_dir(directory)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return final_path, checksum, final_path.stat().st_size


def recover_orphan_parts(settings: Settings, known_paths: set[str]) -> tuple[ManifestEntry, ...]:
    """Adopt published parts that were never recorded in the manifest table.

    Covers a crash between part publication and the manifest transaction; the
    adopted rows keep dedupe/resume working instead of re-downloading forever.
    """
    root = settings.data_dir / "market" / "um" / "agg_trades"
    if not root.is_dir():
        return ()
    recovered: list[ManifestEntry] = []
    for path in sorted(root.glob("symbol=*/date=*/*.parquet")):
        if str(path) in known_paths:
            continue
        symbol = path.parent.parent.name.split("=", 1)[1]
        day = path.parent.name.split("=", 1)[1]
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise StoreUnavailableError(f"cannot reconcile orphan part {path}: {exc}") from exc
        recovered.append(
            ManifestEntry(
                path=str(path),
                data_type="agg_trades",
                symbol=symbol,
                date=day,
                rows=rows,
                bytes=path.stat().st_size,
                checksum=checksum,
                source="recovered:orphan",
                created_at=utc_timestamp(datetime.now(UTC)),
            )
        )
    return tuple(recovered)


def recover_orphan_metric_parts(
    settings: Settings, known_paths: set[str], *, now: str
) -> tuple[ManifestEntry, ...]:
    """Adopt published metrics parts that never reached the manifest table."""
    root = settings.data_dir / "metrics" / "orderflow"
    if not root.is_dir():
        return ()
    recovered: list[ManifestEntry] = []
    for path in sorted(root.glob("symbol=*/period=*/metric_version=*/date=*/*.parquet")):
        if str(path) in known_paths:
            continue
        version = path.parent.parent.name.split("=", 1)[1]
        symbol = path.parent.parent.parent.parent.name.split("=", 1)[1]
        day = path.parent.name.split("=", 1)[1]
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            first = pq.read_table(path).slice(0, 1).to_pylist()
        except (OSError, ValueError, pa.ArrowException) as exc:
            raise StoreUnavailableError(
                f"cannot reconcile orphan metrics part {path}: {exc}"
            ) from exc
        record = first[0] if first else {}
        window_start = record.get("window_start")
        window_end = record.get("window_end")
        recovered.append(
            ManifestEntry(
                path=str(path),
                data_type="metrics",
                symbol=symbol,
                date=day,
                rows=rows,
                bytes=path.stat().st_size,
                checksum=checksum,
                source=f"metrics:{version}",
                created_at=now,
                metric_version=str(record.get("metric_version") or version),
                input_manifest_hash=(
                    str(record["input_manifest_hash"])
                    if record.get("input_manifest_hash") is not None
                    else None
                ),
                window_start=(
                    utc_timestamp(window_start) if isinstance(window_start, datetime) else None
                ),
                window_end=(
                    utc_timestamp(window_end) if isinstance(window_end, datetime) else None
                ),
            )
        )
    return tuple(recovered)


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
        except (OSError, ValueError, pa.ArrowException) as exc:
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
        allow_duplicate_ids: bool = False,
        on_progress: Callable[[int], None] | None = None,
    ) -> tuple[ManifestEntry, ...]:
        """Dedupe by ``agg_trade_id`` and publish one part per UTC date.

        Deduplication is scoped to the batch's date partitions, so re-running
        the same archive range cannot duplicate rows without scanning the whole
        symbol history. A corrected upstream file (new checksum, same ids) sets
        ``allow_duplicate_ids`` so the replacement rows are actually written.
        """
        if not trades:
            return ()
        batch_dates = {trade.transact_time.astimezone(UTC).date().isoformat() for trade in trades}
        seen = set() if allow_duplicate_ids else self._existing_agg_trade_ids(symbol, batch_dates)
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
                if on_progress is not None:
                    on_progress(len(published))
        except Exception:
            # Never leave a partially published multi-date batch behind.
            for entry in published:
                path = Path(entry.path)
                path.unlink(missing_ok=True)
                _fsync_dir(path.parent)
            raise
        return tuple(published)

    def write_metrics_part(
        self,
        rows: tuple[MetricRow, ...] | list[MetricRow],
        *,
        symbol: str,
        period: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry:
        """Publish one immutable long-format metrics part with full provenance.

        An empty (MISSING) window is persisted as a zero-row part so schedules
        crossing data gaps do not fail; readers simply return no rows.
        """
        directory = (
            metrics_root(
                self._settings,
                symbol=symbol,
                period=period,
                metric_version=metric_version,
            )
            / f"date={window_start.astimezone(UTC).date().isoformat()}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        reasons = list(quality_reasons)
        table = pa.table(
            {
                "record_type": pa.array([row.record_type for row in rows], pa.string()),
                "symbol": pa.array([symbol for _ in rows], pa.string()),
                "period": pa.array([period for _ in rows], pa.string()),
                "window_start": pa.array(
                    [window_start.astimezone(UTC) for _ in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "window_end": pa.array(
                    [window_end.astimezone(UTC) for _ in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "bar_start": pa.array(
                    [_utc_or_none(row.bar_start) for row in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "bar_end": pa.array(
                    [_utc_or_none(row.bar_end) for row in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "buy_volume": pa.array(
                    [_optional_decimal(row.buy_volume) for row in rows], pa.string()
                ),
                "sell_volume": pa.array(
                    [_optional_decimal(row.sell_volume) for row in rows], pa.string()
                ),
                "delta": pa.array([_optional_decimal(row.delta) for row in rows], pa.string()),
                "cvd": pa.array([_optional_decimal(row.cvd) for row in rows], pa.string()),
                "taker_buy_ratio": pa.array(
                    [_optional_decimal(row.taker_buy_ratio) for row in rows], pa.string()
                ),
                "price_low": pa.array(
                    [_optional_decimal(row.price_low) for row in rows], pa.string()
                ),
                "price_high": pa.array(
                    [_optional_decimal(row.price_high) for row in rows], pa.string()
                ),
                "volume": pa.array([_optional_decimal(row.volume) for row in rows], pa.string()),
                "is_poc": pa.array([row.is_poc for row in rows], pa.bool_()),
                "in_value_area": pa.array([row.in_value_area for row in rows], pa.bool_()),
                "metric_version": pa.array([metric_version for _ in rows], pa.string()),
                "input_manifest_hash": pa.array([input_manifest_hash for _ in rows], pa.string()),
                "computed_at": pa.array([computed_at for _ in rows], pa.string()),
                "data_quality": pa.array([data_quality for _ in rows], pa.string()),
                "quality_reasons": pa.array([reasons for _ in rows], type=pa.list_(pa.string())),
            }
        )
        # Identity excludes runtime metadata (computed_at): the same inputs,
        # algorithm/params, version, and window always map to one immutable part.
        identity = table.drop_columns(["computed_at"])
        sink = pa.BufferOutputStream()
        pq.write_table(identity, sink, compression="snappy")
        identity_hash = hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()
        prefix = (
            f"{window_start.astimezone(UTC):%Y%m%dT%H%M%S}-"
            f"{window_end.astimezone(UTC):%Y%m%dT%H%M%S}"
        )
        candidate = directory / f"part-{prefix}-{identity_hash[:16]}.parquet"
        if candidate.is_file():
            # Reuse the already-published immutable part (keeps its computed_at).
            final_path = candidate
            checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
            size = candidate.stat().st_size
        else:
            final_path, checksum, size = _publish_table(
                table, directory, name_prefix=prefix, stem_hash=identity_hash
            )
        return ManifestEntry(
            path=str(final_path),
            data_type=METRICS_DATA_TYPE,
            symbol=symbol,
            date=window_start.astimezone(UTC).date().isoformat(),
            rows=len(rows),
            bytes=size,
            checksum=checksum,
            source=f"metrics:{metric_version}",
            created_at=computed_at,
            metric_version=metric_version,
            input_manifest_hash=input_manifest_hash,
            window_start=utc_timestamp(window_start),
            window_end=utc_timestamp(window_end),
        )

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
