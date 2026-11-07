"""Immutable Parquet parts for normalized market events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from oft.config import Settings
from oft.domain.events import (
    AggTrade,
    ForceOrderEvent,
    KlineEvent,
    MarkPriceEvent,
    NormalizedEvent,
)
from oft.domain.metrics.footprint import FootprintResult
from oft.domain.metrics.liquidations import LiquidationEvent, liquidation_sort_key
from oft.domain.metrics.types import MetricRow, decimal_str
from oft.domain.resampling import SOURCE_MODES, Bar
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import ManifestEntry, StorePort, StoreUnavailableError

if TYPE_CHECKING:
    import duckdb

_LAYOUT = ("market", "metrics", "footprint", "liquidations", "bars")
METRICS_DATA_TYPE = "metrics"
FOOTPRINT_DATA_TYPE = "footprint"
LIQUIDATIONS_DATA_TYPE = "liquidations"
BARS_DATA_TYPE = "bars"


def ensure_layout(settings: Settings) -> tuple[Path, ...]:
    """Create the immutable-part directories under the data directory."""
    roots = tuple(settings.data_dir / name for name in _LAYOUT)
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


_VERSION_TOKEN = re.compile(r"^[a-z][a-z0-9_]*\.v[0-9]+\.[0-9a-f]{16}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


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


def footprint_root(settings: Settings, *, symbol: str, period: str, metric_version: str) -> Path:
    """Versioned footprint directory following the ADR-0002 hive layout."""
    _validate_metric_path(symbol, period)
    if _VERSION_TOKEN.fullmatch(metric_version) is None:
        raise ValueError("metric_version contains unsafe path characters")
    return (
        settings.data_dir
        / "footprint"
        / f"symbol={symbol}"
        / f"period={period}"
        / f"metric_version={metric_version}"
    )


def footprint_parts(
    settings: Settings, *, symbol: str, period: str, metric_version: str
) -> tuple[Path, ...]:
    root = footprint_root(settings, symbol=symbol, period=period, metric_version=metric_version)
    if not root.is_dir():
        return ()
    return tuple(sorted(root.glob("date=*/*.parquet")))


def liquidations_root(settings: Settings, *, symbol: str, metric_version: str) -> Path:
    """Versioned liquidation directory; liquidations have no period dimension."""
    if _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("symbol contains unsafe path characters")
    if _VERSION_TOKEN.fullmatch(metric_version) is None:
        raise ValueError("metric_version contains unsafe path characters")
    return (
        settings.data_dir / "liquidations" / f"symbol={symbol}" / f"metric_version={metric_version}"
    )


def liquidation_parts(settings: Settings, *, symbol: str, metric_version: str) -> tuple[Path, ...]:
    root = liquidations_root(settings, symbol=symbol, metric_version=metric_version)
    if not root.is_dir():
        return ()
    return tuple(sorted(root.glob("date=*/*.parquet")))


def bar_root(settings: Settings, *, symbol: str, period: str, metric_version: str) -> Path:
    """Versioned resampled-bar directory following the ADR-0002 hive layout."""
    _validate_metric_path(symbol, period)
    if _VERSION_TOKEN.fullmatch(metric_version) is None:
        raise ValueError("metric_version contains unsafe path characters")
    return (
        settings.data_dir
        / "bars"
        / f"symbol={symbol}"
        / f"period={period}"
        / f"metric_version={metric_version}"
    )


def bar_parts(
    settings: Settings, *, symbol: str, period: str, metric_version: str
) -> tuple[Path, ...]:
    """List published bar parts for one symbol/period/version."""
    root = bar_root(settings, symbol=symbol, period=period, metric_version=metric_version)
    if not root.is_dir():
        return ()
    return tuple(sorted(root.glob("date=*/*.parquet")))


def read_bars(
    settings: Settings,
    *,
    symbol: str,
    period: str,
    metric_version: str,
    run_id: str | None = None,
    store: StorePort | None = None,
) -> tuple[dict[str, object], ...]:
    """Read manifest-registered bars for one run in deterministic order.

    Without ``run_id`` the most recently computed run wins, so a late-event
    recompute run can never be silently mixed with the run it supersedes.
    """
    if store is None:
        from oft.adapters.sqlite_store import SqliteStore

        store = SqliteStore(settings)
    parts = _registered_parts(
        settings,
        store,
        BARS_DATA_TYPE,
        bar_parts(settings, symbol=symbol, period=period, metric_version=metric_version),
    )
    if not parts:
        return ()
    import pyarrow.dataset as dataset

    try:
        table = dataset.dataset([str(path) for path in parts], format="parquet").to_table()
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(f"cannot read bar parts for {symbol}: {exc}") from exc
    rows = cast("list[dict[str, object]]", table.to_pylist())
    if run_id is None:
        run_sequences: dict[str, int] = {}
        for entry in store.list_manifest():
            if entry.data_type != BARS_DATA_TYPE:
                continue
            entry_run_id = entry.source.rsplit(":", 1)[-1]
            run_sequences[entry_run_id] = max(run_sequences.get(entry_run_id, 0), entry.run_seq)
        newest = max(
            (
                (
                    str(row.get("computed_at") or ""),
                    run_sequences.get(str(row.get("run_id")), 0),
                    str(row.get("run_id") or ""),
                )
                for row in rows
                if row.get("run_id") is not None
            ),
            default=("", 0, ""),
        )
        run_id = newest[2]
    rows = [row for row in rows if str(row.get("run_id")) == run_id]
    rows.sort(key=lambda row: str(row.get("bar_start")))
    return _unique_rows(
        rows,
        key=lambda row: (
            row.get("metric_version"),
            row.get("run_id"),
            row.get("bar_start"),
        ),
    )


def _registered_parts(
    settings: Settings,
    store: StorePort | None,
    data_type: str,
    parts: tuple[Path, ...],
) -> tuple[Path, ...]:
    if store is None:
        from oft.adapters.sqlite_store import SqliteStore

        store = SqliteStore(settings)
    store_path = getattr(store, "path", None)
    if store_path is not None and not Path(store_path).exists():
        raise StoreUnavailableError("manifest database is unreadable")
    registered = {
        entry.path
        for entry in store.list_manifest()
        if entry.data_type == data_type and entry.status in {"PUBLISHED", "COMPLETE"}
    }
    return tuple(path for path in parts if str(path) in registered)


def read_footprint(
    settings: Settings,
    *,
    symbol: str,
    period: str,
    metric_version: str,
    store: StorePort | None = None,
) -> tuple[dict[str, object], ...]:
    """Read manifest-registered footprint rows in deterministic price order."""
    parts = _registered_parts(
        settings,
        store,
        FOOTPRINT_DATA_TYPE,
        footprint_parts(settings, symbol=symbol, period=period, metric_version=metric_version),
    )
    if not parts:
        return ()
    import pyarrow.dataset as dataset

    try:
        table = dataset.dataset([str(path) for path in parts], format="parquet").to_table()
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(f"cannot read footprint parts for {symbol}: {exc}") from exc
    rows = cast("list[dict[str, object]]", table.to_pylist())
    rows.sort(
        key=lambda row: (
            str(row.get("window_start")),
            str(row.get("bar_start")),
            Decimal(str(row.get("price", "0"))),
        )
    )
    return _unique_rows(
        rows,
        key=lambda row: (
            row.get("metric_version"),
            row.get("window_start"),
            row.get("bar_start"),
            row.get("price"),
        ),
    )


def read_liquidations(
    settings: Settings,
    *,
    symbol: str,
    metric_version: str,
    store: StorePort | None = None,
) -> tuple[dict[str, object], ...]:
    """Read manifest-registered liquidation event rows in event order."""
    parts = _registered_parts(
        settings,
        store,
        LIQUIDATIONS_DATA_TYPE,
        liquidation_parts(settings, symbol=symbol, metric_version=metric_version),
    )
    if not parts:
        return ()
    import pyarrow.dataset as dataset

    try:
        table = dataset.dataset([str(path) for path in parts], format="parquet").to_table()
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise StoreUnavailableError(f"cannot read liquidation parts for {symbol}: {exc}") from exc
    rows = cast("list[dict[str, object]]", table.to_pylist())
    rows.sort(
        key=lambda row: (
            str(row.get("received_at")),
            str(row.get("trade_time")),
            str(row.get("source_seq")),
        )
    )
    return _unique_rows(
        rows,
        key=lambda row: (
            row.get("metric_version"),
            row.get("received_at"),
            row.get("trade_time"),
            row.get("side"),
            row.get("source_seq"),
            row.get("price"),
            _first_present(row, "original_quantity", "quantity"),
            row.get("average_price"),
            row.get("last_fill_quantity"),
        ),
    )


def _first_present(row: dict[str, object], *names: str) -> object:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _unique_rows(
    rows: list[dict[str, object]],
    *,
    key: Callable[[dict[str, object]], tuple[object, ...]],
) -> tuple[dict[str, object], ...]:
    unique: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        row_key = key(row)
        if row_key in seen:
            continue
        seen.add(row_key)
        unique.append(row)
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

    def write_footprint_part(
        self,
        footprint: FootprintResult,
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
        """Publish one immutable long-format footprint part (ADR-0002 layout)."""
        directory = (
            footprint_root(
                self._settings,
                symbol=symbol,
                period=period,
                metric_version=metric_version,
            )
            / f"date={window_start.astimezone(UTC).date().isoformat()}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        rows = [(bar, cell) for bar in footprint.bars for cell in bar.cells]
        reasons = list(quality_reasons)
        table = pa.table(
            {
                "record_type": pa.array(["FOOTPRINT_CELL" for _ in rows], pa.string()),
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
                    [bar.bar_start.astimezone(UTC) for bar, _ in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "bar_end": pa.array(
                    [bar.bar_end.astimezone(UTC) for bar, _ in rows],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "price": pa.array([decimal_str(cell.price) for _, cell in rows], pa.string()),
                "bid_volume": pa.array(
                    [decimal_str(cell.bid_volume) for _, cell in rows], pa.string()
                ),
                "ask_volume": pa.array(
                    [decimal_str(cell.ask_volume) for _, cell in rows], pa.string()
                ),
                "delta": pa.array([decimal_str(cell.delta) for _, cell in rows], pa.string()),
                "total_volume": pa.array(
                    [decimal_str(cell.total_volume) for _, cell in rows], pa.string()
                ),
                "imbalance_ratio": pa.array(
                    [_optional_decimal(cell.imbalance_ratio) for _, cell in rows],
                    pa.string(),
                ),
                "is_stacked_imbalance": pa.array(
                    [cell.is_stacked_imbalance for _, cell in rows], pa.bool_()
                ),
                "metric_version": pa.array([metric_version for _ in rows], pa.string()),
                "input_manifest_hash": pa.array([input_manifest_hash for _ in rows], pa.string()),
                "computed_at": pa.array([computed_at for _ in rows], pa.string()),
                "data_quality": pa.array([data_quality for _ in rows], pa.string()),
                "quality_reasons": pa.array([reasons for _ in rows], type=pa.list_(pa.string())),
            }
        )
        final_path, checksum, size = self._publish_content_addressed(
            table,
            directory,
            window_start=window_start,
            window_end=window_end,
        )
        return ManifestEntry(
            path=str(final_path),
            data_type=FOOTPRINT_DATA_TYPE,
            symbol=symbol,
            date=window_start.astimezone(UTC).date().isoformat(),
            rows=len(rows),
            bytes=size,
            checksum=checksum,
            source=f"footprint:{metric_version}",
            created_at=computed_at,
            metric_version=metric_version,
            input_manifest_hash=input_manifest_hash,
            window_start=utc_timestamp(window_start),
            window_end=utc_timestamp(window_end),
        )

    def write_liquidation_part(
        self,
        events: Sequence[LiquidationEvent],
        *,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        metric_version: str,
        input_manifest_hash: str,
        computed_at: str,
        data_quality: str,
        quality_reasons: tuple[str, ...],
    ) -> ManifestEntry:
        """Publish one immutable liquidation event part (ADR-0002 layout)."""
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("liquidation window bounds must be timezone-aware")
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        ordered_events = sorted(events, key=liquidation_sort_key)
        for event in ordered_events:
            if event.symbol != symbol:
                raise ValueError(f"liquidation symbol {event.symbol!r} does not match {symbol!r}")
            if not (window_start <= event.received_at < window_end):
                raise ValueError("liquidation event is outside the requested window")
        events = ordered_events
        directory = (
            liquidations_root(self._settings, symbol=symbol, metric_version=metric_version)
            / f"date={window_start.astimezone(UTC).date().isoformat()}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        reasons = list(quality_reasons)
        table = pa.table(
            {
                "received_at": pa.array(
                    [event.received_at.astimezone(UTC) for event in events],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "trade_time": pa.array(
                    [event.trade_time.astimezone(UTC) for event in events],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "symbol": pa.array([event.symbol for event in events], pa.string()),
                "side": pa.array([event.side for event in events], pa.string()),
                "order_type": pa.array([event.order_type for event in events], pa.string()),
                "time_in_force": pa.array([event.time_in_force for event in events], pa.string()),
                "original_quantity": pa.array(
                    [decimal_str(event.quantity) for event in events], pa.string()
                ),
                "price": pa.array([decimal_str(event.price) for event in events], pa.string()),
                "average_price": pa.array(
                    [decimal_str(event.average_price) for event in events], pa.string()
                ),
                "order_status": pa.array([event.order_status for event in events], pa.string()),
                "last_fill_quantity": pa.array(
                    [_optional_decimal(event.last_fill_quantity) for event in events],
                    pa.string(),
                ),
                "accumulated_fill_quantity": pa.array(
                    [decimal_str(event.quantity) for event in events], pa.string()
                ),
                "source_seq": pa.array([event.source_seq for event in events], pa.int64()),
                "metric_version": pa.array([metric_version for _ in events], pa.string()),
                "input_manifest_hash": pa.array([input_manifest_hash for _ in events], pa.string()),
                "computed_at": pa.array([computed_at for _ in events], pa.string()),
                "data_quality": pa.array([data_quality for _ in events], pa.string()),
                "quality_reasons": pa.array([reasons for _ in events], type=pa.list_(pa.string())),
            }
        )
        final_path, checksum, size = self._publish_content_addressed(
            table,
            directory,
            window_start=window_start,
            window_end=window_end,
        )
        return ManifestEntry(
            path=str(final_path),
            data_type=LIQUIDATIONS_DATA_TYPE,
            symbol=symbol,
            date=window_start.astimezone(UTC).date().isoformat(),
            rows=len(events),
            bytes=size,
            checksum=checksum,
            source=f"liquidations:{metric_version}",
            created_at=computed_at,
            metric_version=metric_version,
            input_manifest_hash=input_manifest_hash,
            window_start=utc_timestamp(window_start),
            window_end=utc_timestamp(window_end),
        )

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
    ) -> ManifestEntry:
        """Publish one immutable, content-addressed resampled-bar part.

        The part records ``source_mode``, the input manifest hash, the source
        digest, and the run id so a late-event recompute is auditable and a
        published part is never edited in place.
        """
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("bar window bounds must be timezone-aware")
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        if source_mode not in SOURCE_MODES:
            raise ValueError(f"unsupported source_mode {source_mode!r}")
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("run_id contains unsafe path characters")
        for bar in bars:
            if bar.symbol != symbol:
                raise ValueError(f"bar symbol {bar.symbol!r} does not match {symbol!r}")
            if bar.period != period:
                raise ValueError(f"bar period {bar.period!r} does not match {period!r}")
            if bar.source_mode != source_mode:
                raise ValueError("bar source_mode does not match the part source_mode")
            if not (window_start <= bar.bar_start and bar.bar_end <= window_end):
                raise ValueError("bar is outside the requested window")
        directory = (
            bar_root(self._settings, symbol=symbol, period=period, metric_version=metric_version)
            / f"date={window_start.astimezone(UTC).date().isoformat()}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        reasons = list(quality_reasons)
        table = pa.table(
            {
                "record_type": pa.array(["BAR" for _ in bars], pa.string()),
                "symbol": pa.array([symbol for _ in bars], pa.string()),
                "period": pa.array([period for _ in bars], pa.string()),
                "window_start": pa.array(
                    [window_start.astimezone(UTC) for _ in bars],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "window_end": pa.array(
                    [window_end.astimezone(UTC) for _ in bars],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "bar_start": pa.array(
                    [bar.bar_start.astimezone(UTC) for bar in bars],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "bar_end": pa.array(
                    [bar.bar_end.astimezone(UTC) for bar in bars],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "open": pa.array([decimal_str(bar.open) for bar in bars], pa.string()),
                "high": pa.array([decimal_str(bar.high) for bar in bars], pa.string()),
                "low": pa.array([decimal_str(bar.low) for bar in bars], pa.string()),
                "close": pa.array([decimal_str(bar.close) for bar in bars], pa.string()),
                "volume": pa.array([decimal_str(bar.volume) for bar in bars], pa.string()),
                "quote_volume": pa.array(
                    [decimal_str(bar.quote_volume) for bar in bars], pa.string()
                ),
                "trade_count": pa.array([bar.trade_count for bar in bars], pa.int64()),
                "taker_buy_volume": pa.array(
                    [decimal_str(bar.taker_buy_volume) for bar in bars], pa.string()
                ),
                "source_mode": pa.array([source_mode for _ in bars], pa.string()),
                "metric_version": pa.array([metric_version for _ in bars], pa.string()),
                "input_manifest_hash": pa.array([input_manifest_hash for _ in bars], pa.string()),
                "source_digest": pa.array([source_digest for _ in bars], pa.string()),
                "run_id": pa.array([run_id for _ in bars], pa.string()),
                "computed_at": pa.array([computed_at for _ in bars], pa.string()),
                "data_quality": pa.array([data_quality for _ in bars], pa.string()),
                "quality_reasons": pa.array([reasons for _ in bars], type=pa.list_(pa.string())),
            }
        )
        final_path, checksum, size = self._publish_content_addressed(
            table,
            directory,
            window_start=window_start,
            window_end=window_end,
        )
        return ManifestEntry(
            path=str(final_path),
            data_type=BARS_DATA_TYPE,
            symbol=symbol,
            date=window_start.astimezone(UTC).date().isoformat(),
            rows=len(bars),
            bytes=size,
            checksum=checksum,
            source=f"bars:{metric_version}:{run_id}",
            created_at=computed_at,
            metric_version=metric_version,
            input_manifest_hash=input_manifest_hash,
            window_start=utc_timestamp(window_start),
            window_end=utc_timestamp(window_end),
        )

    def _publish_content_addressed(
        self,
        table: pa.Table,
        directory: Path,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[Path, str, int]:
        """Publish a part whose identity excludes runtime-only ``computed_at``."""
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
            return (
                candidate,
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
                candidate.stat().st_size,
            )
        return _publish_table(table, directory, name_prefix=prefix, stem_hash=identity_hash)

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


@dataclass(frozen=True)
class FlushPolicy:
    """Flush live buffers at 30 s or 50k rows (ADR-0008 defaults)."""

    max_rows: int = 50_000
    max_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if self.max_interval_seconds <= 0:
            raise ValueError("max_interval_seconds must be positive")


LIVE_METRIC_VERSION = "live.v1.0000000000000000"
_LIVE_LAYOUT = {
    "agg_trades": "agg_trades",
    "klines_1m": "klines_1m",
    "mark_price_klines": "mark_price_klines",
}


def _content_digest(table: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="snappy")
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


class LiveParquetBuffer:
    """Buffer normalized live events and publish immutable Parquet parts.

    Parts follow the existing ``temp -> fsync -> rename`` publication path and
    are recorded in the manifest immediately, so a restart never sees a part
    that the manifest does not know about.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        store: StorePort,
        clock: ClockPort,
        policy: FlushPolicy | None = None,
        source: str = "live:ws",
        dedupe_max_keys: int = 200_000,
    ) -> None:
        self._settings = settings
        self._store = store
        self._writer = ParquetStore(settings)
        self._clock = clock
        self._policy = policy or FlushPolicy()
        self._source = source
        self._agg: list[AggTrade] = []
        self._klines: list[KlineEvent] = []
        self._marks: list[MarkPriceEvent] = []
        self._liquidations: list[ForceOrderEvent] = []
        self._last_flush = clock.now()
        self._flushes = 0
        self._rows_written = 0
        self._dedupe_loaded = False
        self._dedupe_max_keys = dedupe_max_keys
        # LRU-bounded: (symbol, key) -> ``is_closed`` for klines, True for marks.
        self._seen_klines: OrderedDict[tuple[str, object], bool] = OrderedDict()
        self._seen_marks: OrderedDict[tuple[str, object], bool] = OrderedDict()
        self._seen_agg_ids: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._retry_persistent_dedupe = False
        self._pending_marks: list[tuple[str, tuple[object, ...], bool]] = []
        self._uncommitted_entries: list[ManifestEntry] = []
        self._max_uncommitted_entries = 10_000
        self._last_commit = False
        # Open bars are not durable: keep only the latest in memory until closed.
        self._open_klines: dict[tuple[str, object], object] = {}
        self._logger = structlog.get_logger(__name__)

    def _apply_pending_marks(self) -> None:
        for kind, raw_key, closed in self._pending_marks:
            if kind == "agg":
                agg_key = cast("tuple[str, int]", raw_key)
                self._seen_agg_ids.pop(agg_key, None)
                self._seen_agg_ids[agg_key] = None
                while len(self._seen_agg_ids) > self._dedupe_max_keys:
                    self._seen_agg_ids.popitem(last=False)
            elif kind == "kline":
                self._remember_key(self._seen_klines, cast("tuple[str, object]", raw_key), closed)
            else:
                self._remember_key(self._seen_marks, cast("tuple[str, object]", raw_key), True)
        self._pending_marks.clear()

    @property
    def last_flush_committed(self) -> bool:
        return self._last_commit

    @property
    def dedupe_keys(self) -> int:
        return len(self._seen_klines) + len(self._seen_marks) + len(self._seen_agg_ids)

    def _remember_key(
        self, keys: OrderedDict[tuple[str, object], bool], key: tuple[str, object], closed: bool
    ) -> None:
        if key in keys:
            keys.pop(key)
        keys[key] = closed
        while len(keys) > self._dedupe_max_keys:
            keys.popitem(last=False)

    def _load_existing_live_keys(self) -> None:
        """Load persisted kline/mark keys so a lost checkpoint cannot duplicate."""
        if self._dedupe_loaded:
            return
        for entry in self._store.list_manifest():
            try:
                if entry.data_type == "agg_trades":
                    table = pq.read_table(entry.path, columns=["symbol", "agg_trade_id"])
                    for row in table.to_pylist():
                        if row["agg_trade_id"] is not None:
                            key = (str(row["symbol"]), int(row["agg_trade_id"]))
                            self._seen_agg_ids.pop(key, None)
                            self._seen_agg_ids[key] = None
                            while len(self._seen_agg_ids) > self._dedupe_max_keys:
                                self._seen_agg_ids.popitem(last=False)
                elif entry.data_type == "klines_1m":
                    table = pq.read_table(entry.path, columns=["symbol", "open_time", "is_closed"])
                    for row in table.to_pylist():
                        key = (str(row["symbol"]), row["open_time"])
                        if self._seen_klines.get(key) is not True:
                            self._remember_key(self._seen_klines, key, bool(row["is_closed"]))
                elif entry.data_type == "mark_price_klines":
                    table = pq.read_table(entry.path, columns=["symbol", "event_time"])
                    for row in table.to_pylist():
                        self._remember_key(
                            self._seen_marks,
                            (str(row["symbol"]), row["event_time"]),
                            True,
                        )
            except (OSError, ValueError, pa.ArrowException) as exc:
                raise StoreUnavailableError(
                    f"cannot read existing live part {entry.path}: {exc}"
                ) from exc
        self._dedupe_loaded = True

    @property
    def pending_rows(self) -> int:
        return len(self._agg) + len(self._klines) + len(self._marks) + len(self._liquidations)

    @property
    def max_rows(self) -> int:
        return self._policy.max_rows

    def is_due(self, now: datetime | None = None) -> bool:
        """True when the interval budget elapsed since the last flush."""
        instant = now or self._clock.now()
        return (instant - self._last_flush).total_seconds() >= self._policy.max_interval_seconds

    @property
    def stats(self) -> dict[str, int]:
        return {
            "flushes": self._flushes,
            "rows_written": self._rows_written,
            "pending_rows": self.pending_rows,
        }

    def add(self, event: NormalizedEvent) -> None:
        if isinstance(event, AggTrade):
            self._agg.append(event)
        elif isinstance(event, KlineEvent):
            self._klines.append(event)
        elif isinstance(event, MarkPriceEvent):
            self._marks.append(event)
        elif isinstance(event, ForceOrderEvent):
            self._liquidations.append(event)
        else:  # pragma: no cover - the union is closed
            raise TypeError(f"unsupported live event: {type(event)!r}")

    def should_flush(self, now: datetime | None = None) -> bool:
        if self.pending_rows == 0:
            return False
        if self.pending_rows >= self._policy.max_rows:
            return True
        return self.is_due(now)

    def maybe_flush(self, now: datetime | None = None) -> tuple[ManifestEntry, ...]:
        return self.flush() if self.should_flush(now) else ()

    def flush(self) -> tuple[ManifestEntry, ...]:
        """Publish every buffered stream type and record manifest rows."""
        self._load_existing_live_keys()
        self._pending_marks = []
        self._last_commit = False
        entries: list[ManifestEntry] = []
        entries.extend(self._write_agg_trades(self._agg))
        entries.extend(self._write_klines(self._klines))
        entries.extend(self._write_mark_prices(self._marks))
        entries.extend(self._write_liquidations(self._liquidations))
        try:
            if self._uncommitted_entries:
                # Parts published by a previous failed flush: record them now,
                # before the retry dedupes against those same parts.
                self._store.upsert_manifest_entries(tuple(self._uncommitted_entries))
                self._uncommitted_entries = []
                self._last_commit = True
            if entries:
                self._store.upsert_manifest_entries(tuple(entries))
                self._last_commit = True
        except Exception:
            # Manifest did not commit: keep buffers/parts and do NOT mark keys
            # seen, so a retry can record them and re-attempt the data.
            combined = [*self._uncommitted_entries, *entries]
            if len(combined) > self._max_uncommitted_entries:
                raise StoreUnavailableError(
                    "uncommitted published parts exceeded the retry cap; "
                    "manual recovery is required"
                ) from None
            self._uncommitted_entries = combined
            self._retry_persistent_dedupe = True
            self._pending_marks = []
            raise
        self._apply_pending_marks()
        self._agg.clear()
        self._klines.clear()
        self._marks.clear()
        self._liquidations.clear()
        self._last_flush = self._clock.now()
        self._flushes += 1
        self._rows_written += sum(entry.rows for entry in entries)
        return tuple(entries)

    def _write_agg_trades(self, trades: list[AggTrade]) -> list[ManifestEntry]:
        if not trades:
            return []
        published: list[ManifestEntry] = []
        by_symbol: dict[str, list[AggTrade]] = {}
        for trade in trades:
            by_symbol.setdefault(trade.symbol, []).append(trade)
        for symbol, symbol_trades in by_symbol.items():
            unique: list[AggTrade] = []
            new_keys: list[tuple[str, int]] = []
            for trade in symbol_trades:
                key = (trade.symbol, trade.agg_trade_id)
                if key in self._seen_agg_ids:
                    continue
                unique.append(trade)
                new_keys.append(key)
            if not unique:
                continue
            try:
                # A retry after a partial publish falls back to the persistent
                # dedupe backstop; the success path stays incremental.
                published.extend(
                    self._writer.write_agg_trades_batch(
                        unique,
                        symbol=symbol,
                        source=self._source,
                        allow_duplicate_ids=not self._retry_persistent_dedupe,
                    )
                )
            except Exception:
                self._retry_persistent_dedupe = True
                raise
            else:
                self._retry_persistent_dedupe = False
                self._pending_marks.extend(("agg", key, True) for key in new_keys)
        return published

    def _write_klines(self, klines: list[KlineEvent]) -> list[ManifestEntry]:
        if not klines:
            return []
        published: list[ManifestEntry] = []
        by_symbol: dict[str, list[KlineEvent]] = {}
        for event in klines:
            by_symbol.setdefault(event.symbol, []).append(event)
        for symbol, events in by_symbol.items():
            by_day: dict[str, list[KlineEvent]] = {}
            for event in events:
                day = event.open_time.astimezone(UTC).date().isoformat()
                by_day.setdefault(day, []).append(event)
            for day, day_events in by_day.items():
                pending: dict[tuple[str, object], KlineEvent] = {}
                for event in day_events:
                    key: tuple[str, object] = (symbol, event.open_time)
                    if self._seen_klines.get(key) is True:
                        continue  # already final
                    if not event.is_closed:
                        # Keep the newest open bar in memory; persist only closed
                        # bars so the same open_time can never yield two rows.
                        self._open_klines[key] = event
                        continue
                    self._open_klines.pop(key, None)
                    pending[key] = event  # last closed update wins
                if not pending:
                    continue
                day_events = list(pending.values())
                table = pa.table(
                    {
                        "event_time": pa.array(
                            [event.event_time.astimezone(UTC) for event in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                        "open_time": pa.array(
                            [event.open_time.astimezone(UTC) for event in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                        "close_time": pa.array(
                            [event.close_time.astimezone(UTC) for event in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                        "symbol": pa.array([symbol for _ in day_events], pa.string()),
                        "interval": pa.array([event.interval for event in day_events], pa.string()),
                        "open": pa.array([str(event.open) for event in day_events], pa.string()),
                        "high": pa.array([str(event.high) for event in day_events], pa.string()),
                        "low": pa.array([str(event.low) for event in day_events], pa.string()),
                        "close": pa.array([str(event.close) for event in day_events], pa.string()),
                        "volume": pa.array(
                            [str(event.volume) for event in day_events], pa.string()
                        ),
                        "quote_volume": pa.array(
                            [str(event.quote_volume) for event in day_events], pa.string()
                        ),
                        "trade_count": pa.array(
                            [event.trade_count for event in day_events], pa.int64()
                        ),
                        "taker_buy_volume": pa.array(
                            [str(event.taker_buy_volume) for event in day_events], pa.string()
                        ),
                        "taker_buy_quote_volume": pa.array(
                            [str(event.taker_buy_quote_volume) for event in day_events],
                            pa.string(),
                        ),
                        "is_closed": pa.array(
                            [event.is_closed for event in day_events], pa.bool_()
                        ),
                        "source": pa.array([self._source for _ in day_events], pa.string()),
                        "ingested_at": pa.array(
                            [self._clock.now().astimezone(UTC) for _ in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                    }
                )
                published.append(
                    self._publish_live_part(
                        table,
                        data_type="klines_1m",
                        symbol=symbol,
                        day=day,
                        prefix=f"{day}-kline",
                    )
                )
                self._pending_marks.extend(
                    ("kline", key, event.is_closed) for key, event in pending.items()
                )
        return published

    def _write_mark_prices(self, marks: list[MarkPriceEvent]) -> list[ManifestEntry]:
        if not marks:
            return []
        published: list[ManifestEntry] = []
        by_symbol: dict[str, list[MarkPriceEvent]] = {}
        for event in marks:
            by_symbol.setdefault(event.symbol, []).append(event)
        for symbol, events in by_symbol.items():
            by_day: dict[str, list[MarkPriceEvent]] = {}
            for event in events:
                day = event.event_time.astimezone(UTC).date().isoformat()
                by_day.setdefault(day, []).append(event)
            for day, day_events in by_day.items():
                pending_marks: dict[tuple[str, object], MarkPriceEvent] = {}
                for event in day_events:
                    key: tuple[str, object] = (symbol, event.event_time)
                    if key in self._seen_marks:
                        continue
                    pending_marks[key] = event
                if not pending_marks:
                    continue
                day_events = list(pending_marks.values())
                table = pa.table(
                    {
                        "event_time": pa.array(
                            [event.event_time.astimezone(UTC) for event in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                        "symbol": pa.array([symbol for _ in day_events], pa.string()),
                        "mark_price": pa.array(
                            [str(event.mark_price) for event in day_events], pa.string()
                        ),
                        "index_price": pa.array(
                            [str(event.index_price) for event in day_events], pa.string()
                        ),
                        "funding_rate": pa.array(
                            [str(event.funding_rate) for event in day_events], pa.string()
                        ),
                        "next_funding_time": pa.array(
                            [
                                event.next_funding_time.astimezone(UTC)
                                if event.next_funding_time is not None
                                else None
                                for event in day_events
                            ],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                        "source": pa.array([self._source for _ in day_events], pa.string()),
                        "ingested_at": pa.array(
                            [self._clock.now().astimezone(UTC) for _ in day_events],
                            pa.timestamp("ms", tz="UTC"),
                        ),
                    }
                )
                published.append(
                    self._publish_live_part(
                        table,
                        data_type="mark_price_klines",
                        symbol=symbol,
                        day=day,
                        prefix=f"{day}-mark",
                    )
                )
                self._pending_marks.extend(("mark", key, True) for key in pending_marks)
        return published

    def _write_liquidations(self, events: list[ForceOrderEvent]) -> list[ManifestEntry]:
        if not events:
            return []
        published: list[ManifestEntry] = []
        by_symbol: dict[str, list[ForceOrderEvent]] = {}
        for event in events:
            by_symbol.setdefault(event.symbol, []).append(event)
        writer = self._writer
        for symbol, symbol_events in by_symbol.items():
            normalized = sorted(
                (self._liquidation_event(event) for event in symbol_events),
                key=liquidation_sort_key,
            )
            window_start = min(event.received_at for event in normalized)
            window_end = max(event.received_at for event in normalized) + timedelta(milliseconds=1)
            published.append(
                writer.write_liquidation_part(
                    normalized,
                    symbol=symbol,
                    window_start=window_start,
                    window_end=window_end,
                    metric_version=LIVE_METRIC_VERSION,
                    input_manifest_hash="live",
                    computed_at=utc_timestamp(self._clock.now()),
                    data_quality="COMPLETE",
                    quality_reasons=(),
                )
            )
        return published

    def _liquidation_event(self, event: ForceOrderEvent) -> LiquidationEvent:
        return LiquidationEvent(
            received_at=event.event_time,
            trade_time=event.trade_time,
            symbol=event.symbol,
            side=event.side,
            price=event.price,
            average_price=event.average_price,
            quantity=event.quantity,
            order_type=event.order_type,
            time_in_force=event.time_in_force,
            order_status=event.order_status,
            last_fill_quantity=event.last_fill_quantity,
            source_seq=event.source_seq,
        )

    def _publish_live_part(
        self,
        table: pa.Table,
        *,
        data_type: str,
        symbol: str,
        day: str,
        prefix: str,
    ) -> ManifestEntry:
        directory = (
            self._settings.data_dir
            / "market"
            / "um"
            / _LIVE_LAYOUT[data_type]
            / f"symbol={symbol}"
            / f"date={day}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        digest = _content_digest(table)
        final_path, checksum, size = _publish_table(
            table, directory, name_prefix=prefix, stem_hash=digest
        )
        return ManifestEntry(
            path=str(final_path),
            data_type=data_type,
            symbol=symbol,
            date=day,
            rows=table.num_rows,
            bytes=size,
            checksum=checksum,
            source=self._source,
            created_at=utc_timestamp(self._clock.now()),
        )


def quarantine_orphan_live_parts(
    settings: Settings,
    store: StorePort,
    *,
    clock: ClockPort | None = None,
) -> tuple[Path, ...]:
    """Move live Parquet parts with no manifest row into ``quarantine/``.

    A crash between the atomic rename and the manifest transaction leaves an
    unregistered part; adopting it silently would hide the failure, so it is
    moved aside and logged instead.
    """
    registered = {entry.path for entry in store.list_manifest()}
    roots = [
        settings.data_dir / "market" / "um" / name
        for name in ("agg_trades", "klines_1m", "mark_price_klines")
    ]
    roots.append(settings.data_dir / "liquidations")
    quarantined: list[Path] = []
    logger = structlog.get_logger(__name__)
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("**/part-*.parquet")):
            if str(path) in registered or not path.is_file():
                continue
            relative = path.relative_to(settings.data_dir)
            destination = settings.data_dir / "quarantine" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            _fsync_dir(destination.parent)
            quarantined.append(destination)
            logger.warning(
                "orphan_part_quarantined",
                path=str(relative),
                quarantined_as=str(destination.relative_to(settings.data_dir)),
                observed_at=utc_timestamp((clock or _SystemClock()).now()),
            )
    return tuple(quarantined)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
