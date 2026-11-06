"""SQLite transactional store (WAL) for scans and the Parquet manifest.

The daemon is the only writer. Diagnostic commands open the database with
``mode=ro`` so ``oft status`` / ``oft doctor`` never block or create files.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from oft.config import Settings
from oft.domain.data_quality import DataGap, GapSeverity
from oft.domain.symbols import SymbolContract
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    BackfillManifestRow,
    BackfillRunRecord,
    ManifestEntry,
    ScanRecord,
    StoreUnavailableError,
    SymbolUpsertResult,
)

_SCHEMA_VERSION = 5
STALE_RUNNING_SECONDS = 15 * 60
_BACKFILL_RUN_STATUSES = ("RUNNING", "COMPLETE", "PARTIAL", "FAILED", "BLOCKED", "STALE")
_BACKFILL_RUNS_DEFINITION = """
    run_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('RUNNING','COMPLETE','PARTIAL','FAILED','BLOCKED','STALE')
    ),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner TEXT,
    finished_at TEXT,
    estimated_bytes INTEGER NOT NULL,
    processed_bytes INTEGER NOT NULL,
    files_total INTEGER NOT NULL,
    files_verified INTEGER NOT NULL,
    files_failed INTEGER NOT NULL,
    error_code TEXT,
    message TEXT
"""

_BACKFILL_MANIFEST_DEFINITION = """
    file_key TEXT PRIMARY KEY,
    run_id TEXT REFERENCES backfill_runs(run_id),
    data_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    checksum TEXT NOT NULL,
    checksum_verified INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETE','FAILED','STALE','BLOCKED')),
    bytes INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    downloaded_at TEXT,
    verified_at TEXT,
    error_code TEXT,
    CHECK (checksum_verified IN (0, 1))
"""

_DATA_GAPS_DEFINITION = """
    gap_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL,
    gap_start TEXT NOT NULL,
    gap_end TEXT NOT NULL,
    reason TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('WARNING','CRITICAL')),
    detected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    run_id TEXT REFERENCES backfill_runs(run_id)
"""


def _like_pattern(query: str) -> str:
    """Escape LIKE metacharacters so a literal ``_``/``%`` does not match all."""
    escaped = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "oft.db"


_SYMBOLS_COLUMNS = (
    "symbol",
    "base_asset",
    "quote_asset",
    "contract_type",
    "underlying_type",
    "status",
    "tick_size",
    "step_size",
    "min_qty",
    "min_notional",
    "price_precision",
    "quantity_precision",
    "onboard_date",
    "tradifi",
    "updated_at",
)
_SYMBOLS_DEFINITION = """
    symbol TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    underlying_type TEXT,
    status TEXT NOT NULL,
    tick_size TEXT NOT NULL,
    step_size TEXT NOT NULL,
    min_qty TEXT NOT NULL,
    min_notional TEXT NOT NULL,
    price_precision INTEGER NOT NULL,
    quantity_precision INTEGER NOT NULL,
    onboard_date INTEGER NOT NULL,
    tradifi INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (tradifi IN (0, 1)),
    CHECK (price_precision >= 0 AND quantity_precision >= 0),
    CHECK (onboard_date > 0)
"""
_SYMBOL_INDEXES = (
    "symbols_contract_type",
    "symbols_tradifi",
    "symbols_tradifi_symbol",
    "symbols_contract_tradifi_symbol",
)
_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scans (
        scan_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        period TEXT NOT NULL,
        status TEXT NOT NULL,
        trigger_source TEXT NOT NULL,
        metrics_only INTEGER NOT NULL,
        fixture_path TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        trade_count INTEGER NOT NULL,
        buy_volume TEXT NOT NULL,
        sell_volume TEXT NOT NULL,
        delta TEXT NOT NULL,
        cvd TEXT NOT NULL,
        data_quality TEXT NOT NULL,
        manifest_path TEXT NOT NULL,
        parquet_rows INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS scans_created_at ON scans(created_at);
    CREATE TABLE IF NOT EXISTS manifest (
        path TEXT PRIMARY KEY,
        data_type TEXT NOT NULL,
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        rows INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS symbols ({_SYMBOLS_DEFINITION});
    CREATE INDEX IF NOT EXISTS symbols_contract_type ON symbols(contract_type);
    CREATE INDEX IF NOT EXISTS symbols_tradifi ON symbols(tradifi);
    CREATE INDEX IF NOT EXISTS symbols_tradifi_symbol ON symbols(tradifi, symbol);
    CREATE INDEX IF NOT EXISTS symbols_contract_tradifi_symbol
        ON symbols(contract_type, tradifi, symbol);
    CREATE TABLE IF NOT EXISTS backfill_runs ({_BACKFILL_RUNS_DEFINITION});
    CREATE INDEX IF NOT EXISTS backfill_runs_status ON backfill_runs(status);
    CREATE TABLE IF NOT EXISTS backfill_manifest ({_BACKFILL_MANIFEST_DEFINITION});
    CREATE INDEX IF NOT EXISTS backfill_manifest_series
        ON backfill_manifest(symbol, data_type, date);
    CREATE INDEX IF NOT EXISTS backfill_manifest_run ON backfill_manifest(run_id);
    CREATE TABLE IF NOT EXISTS data_gaps ({_DATA_GAPS_DEFINITION});
    CREATE INDEX IF NOT EXISTS data_gaps_series ON data_gaps(symbol, data_type, gap_start);
    CREATE INDEX IF NOT EXISTS manifest_symbol_date ON manifest(symbol, date);
"""


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _schema_version(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "schema_migrations"):
        return 0
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _migrate_symbols_v2(connection: sqlite3.Connection) -> None:
    """Rebuild the v1 symbols table so its CHECK constraints actually apply."""
    columns = ", ".join(_SYMBOLS_COLUMNS)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for index in _SYMBOL_INDEXES:
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute("DROP TABLE IF EXISTS symbols_v1")
        connection.execute("ALTER TABLE symbols RENAME TO symbols_v1")
        connection.execute(f"CREATE TABLE symbols ({_SYMBOLS_DEFINITION})")
        connection.execute(f"INSERT INTO symbols ({columns}) SELECT {columns} FROM symbols_v1")
        connection.execute("DROP TABLE symbols_v1")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_backfill_v4(connection: sqlite3.Connection) -> None:
    """Rebuild ``backfill_runs`` with NOT NULL heartbeat, owner, and CHECKs."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(backfill_runs)")}
    updated_expr = (
        "COALESCE(updated_at, finished_at, started_at)"
        if "updated_at" in existing
        else "COALESCE(finished_at, started_at)"
    )
    owner_expr = "owner" if "owner" in existing else "NULL"
    status_check = ",".join(f"'{status}'" for status in _BACKFILL_RUN_STATUSES)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP INDEX IF EXISTS backfill_runs_status")
        connection.execute("DROP TABLE IF EXISTS backfill_runs_v3")
        connection.execute("ALTER TABLE backfill_runs RENAME TO backfill_runs_v3")
        connection.execute(f"CREATE TABLE backfill_runs ({_BACKFILL_RUNS_DEFINITION})")
        connection.execute(
            f"""
            INSERT INTO backfill_runs (
                run_id, symbol, data_type, start_date, end_date, status,
                started_at, updated_at, owner, finished_at, estimated_bytes,
                processed_bytes, files_total, files_verified, files_failed,
                error_code, message
            )
            SELECT run_id, symbol, data_type, start_date, end_date,
                CASE WHEN status IN ({status_check}) THEN status ELSE 'FAILED' END,
                started_at, {updated_expr}, {owner_expr}, finished_at,
                estimated_bytes, processed_bytes, files_total, files_verified,
                files_failed, error_code, message
            FROM backfill_runs_v3
            """
        )
        connection.execute("DROP TABLE backfill_runs_v3")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_checks_v5(connection: sqlite3.Connection) -> None:
    """Add CHECKs/FKs to backfill_manifest and data_gaps (v5).

    Existing rows are copied as-is; FK enforcement is disabled for the copy so
    a legacy dangling run_id cannot block startup, then re-enabled.
    """
    manifest_columns = (
        "file_key",
        "run_id",
        "data_type",
        "symbol",
        "date",
        "source_url",
        "checksum",
        "checksum_verified",
        "status",
        "bytes",
        "rows",
        "downloaded_at",
        "verified_at",
        "error_code",
    )
    gap_columns = (
        "gap_id",
        "symbol",
        "data_type",
        "gap_start",
        "gap_end",
        "reason",
        "severity",
        "detected_at",
        "source",
        "resolved_at",
        "resolution",
        "run_id",
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(connection, "backfill_manifest"):
            connection.execute("DROP INDEX IF EXISTS backfill_manifest_series")
            connection.execute("DROP INDEX IF EXISTS backfill_manifest_run")
            connection.execute("DROP TABLE IF EXISTS backfill_manifest_v4")
            connection.execute("ALTER TABLE backfill_manifest RENAME TO backfill_manifest_v4")
            connection.execute(f"CREATE TABLE backfill_manifest ({_BACKFILL_MANIFEST_DEFINITION})")
            joined = ", ".join(manifest_columns)
            connection.execute(
                f"INSERT INTO backfill_manifest ({joined}) "
                f"SELECT {joined} FROM backfill_manifest_v4"
            )
            connection.execute("DROP TABLE backfill_manifest_v4")
        if _table_exists(connection, "data_gaps"):
            connection.execute("DROP INDEX IF EXISTS data_gaps_series")
            connection.execute("DROP TABLE IF EXISTS data_gaps_v4")
            connection.execute("ALTER TABLE data_gaps RENAME TO data_gaps_v4")
            connection.execute(f"CREATE TABLE data_gaps ({_DATA_GAPS_DEFINITION})")
            joined = ", ".join(gap_columns)
            connection.execute(
                f"INSERT INTO data_gaps ({joined}) SELECT {joined} FROM data_gaps_v4"
            )
            connection.execute("DROP TABLE data_gaps_v4")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def initialize_database(settings: Settings) -> None:
    """Create the data directory and the walking-skeleton schema."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    db_path = database_path(settings)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        if _schema_version(connection) < 2 and _table_exists(connection, "symbols"):
            _migrate_symbols_v2(connection)
        if _schema_version(connection) < 4 and _table_exists(connection, "backfill_runs"):
            _migrate_backfill_v4(connection)
        if _schema_version(connection) < 5 and (
            _table_exists(connection, "backfill_manifest") or _table_exists(connection, "data_gaps")
        ):
            _migrate_checks_v5(connection)
        with connection:
            connection.executescript(_SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (1, "2026-11-06T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (2, "2026-11-06T12:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (3, "2026-11-06T15:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (4, "2026-11-06T16:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, "2026-11-06T18:00:00.000Z"),
            )
    finally:
        connection.close()
        # The DB may hold a trading journal; never leave it group/world readable.
        os.chmod(db_path, 0o600)


def _row_to_scan(row: sqlite3.Row) -> ScanRecord:
    return ScanRecord(
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        period=row["period"],
        status=row["status"],
        trigger_source=row["trigger_source"],
        metrics_only=bool(row["metrics_only"]),
        fixture_path=row["fixture_path"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        trade_count=row["trade_count"],
        buy_volume=row["buy_volume"],
        sell_volume=row["sell_volume"],
        delta=row["delta"],
        cvd=row["cvd"],
        data_quality=row["data_quality"],
        manifest_path=row["manifest_path"],
        parquet_rows=row["parquet_rows"],
        created_at=row["created_at"],
    )


def _row_to_manifest(row: sqlite3.Row) -> ManifestEntry:
    return ManifestEntry(
        path=row["path"],
        data_type=row["data_type"],
        symbol=row["symbol"],
        date=row["date"],
        rows=row["rows"],
        bytes=row["bytes"],
        checksum=row["checksum"],
        source=row["source"],
        created_at=row["created_at"],
        status=row["status"],
    )


_SYMBOL_SPEC_COLUMNS = _SYMBOLS_COLUMNS[:-1]


def _row_to_symbol(row: sqlite3.Row) -> SymbolContract:
    return SymbolContract(
        symbol=row["symbol"],
        base_asset=row["base_asset"],
        quote_asset=row["quote_asset"],
        contract_type=row["contract_type"],
        underlying_type=row["underlying_type"],
        status=row["status"],
        tick_size=Decimal(row["tick_size"]),
        step_size=Decimal(row["step_size"]),
        min_qty=Decimal(row["min_qty"]),
        min_notional=Decimal(row["min_notional"]),
        price_precision=row["price_precision"],
        quantity_precision=row["quantity_precision"],
        onboard_date=row["onboard_date"],
        tradifi=bool(row["tradifi"]),
    )


def _symbol_values(contract: SymbolContract) -> tuple[object, ...]:
    return (
        contract.symbol,
        contract.base_asset,
        contract.quote_asset,
        contract.contract_type,
        contract.underlying_type,
        contract.status,
        str(contract.tick_size),
        str(contract.step_size),
        str(contract.min_qty),
        str(contract.min_notional),
        contract.price_precision,
        contract.quantity_precision,
        contract.onboard_date,
        int(contract.tradifi),
    )


def _row_to_backfill_run(row: sqlite3.Row) -> BackfillRunRecord:
    return BackfillRunRecord(
        run_id=row["run_id"],
        symbol=row["symbol"],
        data_type=row["data_type"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        estimated_bytes=row["estimated_bytes"],
        processed_bytes=row["processed_bytes"],
        files_total=row["files_total"],
        files_verified=row["files_verified"],
        files_failed=row["files_failed"],
        error_code=row["error_code"],
        message=row["message"],
    )


def _row_to_backfill_manifest(row: sqlite3.Row) -> BackfillManifestRow:
    return BackfillManifestRow(
        file_key=row["file_key"],
        run_id=row["run_id"],
        data_type=row["data_type"],
        symbol=row["symbol"],
        date=row["date"],
        source_url=row["source_url"],
        checksum=row["checksum"],
        checksum_verified=bool(row["checksum_verified"]),
        status=row["status"],
        bytes=row["bytes"],
        rows=row["rows"],
        downloaded_at=row["downloaded_at"],
        verified_at=row["verified_at"],
        error_code=row["error_code"],
    )


def _row_to_data_gap(row: sqlite3.Row) -> DataGap:
    return DataGap(
        gap_id=row["gap_id"],
        symbol=row["symbol"],
        data_type=row["data_type"],
        start=row["gap_start"],
        end=row["gap_end"],
        reason=row["reason"],
        severity=GapSeverity(row["severity"]),
        detected_at=row["detected_at"],
        source=row["source"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        run_id=row["run_id"],
    )


class SqliteStore:
    """Concrete :class:`oft.ports.store.StorePort` implementation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = database_path(settings)

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        initialize_database(self._settings)

    def _connect(self) -> sqlite3.Connection:
        settings = self._settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection | None:
        if not self._path.exists():
            return None
        connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def save_scan(self, record: ScanRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_scan(connection, record)

    def _insert_scan(self, connection: sqlite3.Connection, record: ScanRecord) -> None:
        connection.execute(
            """
            INSERT INTO scans (
                scan_id, symbol, period, status, trigger_source, metrics_only,
                fixture_path, started_at, finished_at, window_start, window_end,
                trade_count, buy_volume, sell_volume, delta, cvd, data_quality,
                manifest_path, parquet_rows, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scan_id,
                record.symbol,
                record.period,
                record.status,
                record.trigger_source,
                int(record.metrics_only),
                record.fixture_path,
                record.started_at,
                record.finished_at,
                record.window_start,
                record.window_end,
                record.trade_count,
                record.buy_volume,
                record.sell_volume,
                record.delta,
                record.cvd,
                record.data_quality,
                record.manifest_path,
                record.parquet_rows,
                record.created_at,
            ),
        )

    def save_scan_with_manifest(self, record: ScanRecord, entry: ManifestEntry) -> None:
        """Persist the scan row and manifest row in one transaction (unit of work)."""
        with closing(self._connect()) as connection, connection:
            self._insert_scan(connection, record)
            self._insert_manifest(connection, entry)

    def get_scan(self, scan_id: str) -> ScanRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        finally:
            connection.close()
        return _row_to_scan(row) if row is not None else None

    def latest_scan(self) -> ScanRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM scans ORDER BY created_at DESC, scan_id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_scan(row) if row is not None else None

    def list_scans(self, limit: int = 20) -> tuple[ScanRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scans ORDER BY created_at DESC, scan_id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan(row) for row in rows)

    def record_manifest(self, entry: ManifestEntry) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_manifest(connection, entry)

    def _insert_manifest(self, connection: sqlite3.Connection, entry: ManifestEntry) -> None:
        connection.execute(
            """
            INSERT INTO manifest (
                path, data_type, symbol, date, rows, bytes, checksum,
                source, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.path,
                entry.data_type,
                entry.symbol,
                entry.date,
                entry.rows,
                entry.bytes,
                entry.checksum,
                entry.source,
                entry.created_at,
                entry.status,
            ),
        )

    def list_manifest(
        self, *, symbol: str | None = None, date: str | None = None
    ) -> tuple[ManifestEntry, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if date is not None:
            clauses.append("date = ?")
            params.append(date)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM manifest{where} ORDER BY created_at, path", params
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_manifest(row) for row in rows)

    def integrity_check(self) -> bool:
        connection = self._connect_read_only()
        if connection is None:
            return False
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()
        return bool(row is not None and row[0] == "ok")

    # -- Symbol registry -------------------------------------------------

    def upsert_symbols(
        self, contracts: tuple[SymbolContract, ...], *, observed_at: str
    ) -> SymbolUpsertResult:
        """Insert or update contracts; ``updated_at`` moves only when the spec changed."""
        inserted = updated = unchanged = 0
        columns = ", ".join(_SYMBOLS_COLUMNS)
        placeholders = ", ".join("?" for _ in _SYMBOLS_COLUMNS)
        assignments = ", ".join(
            f"{column} = excluded.{column}" for column in _SYMBOL_SPEC_COLUMNS[1:]
        )
        upsert_sql = (
            f"INSERT INTO symbols ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {assignments}, "
            "updated_at = excluded.updated_at"
        )
        connection = None
        try:
            connection = self._connect()
            for contract in contracts:
                row = connection.execute(
                    "SELECT * FROM symbols WHERE symbol = ?", (contract.symbol,)
                ).fetchone()
                if row is not None and _row_to_symbol(row) == contract:
                    unchanged += 1
                    continue
                connection.execute(upsert_sql, (*_symbol_values(contract), observed_at))
                if row is None:
                    inserted += 1
                else:
                    updated += 1
            total_row = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise StoreUnavailableError(f"symbol registry write failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
        return SymbolUpsertResult(
            inserted=inserted, updated=updated, unchanged=unchanged, total=total
        )

    def _symbol_query(
        self,
        *,
        query: str | None,
        tradifi: bool | None,
        contract_type: str | None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if query:
            pattern = _like_pattern(query)
            clauses.append(
                "(LOWER(symbol) LIKE ? ESCAPE '\\' OR LOWER(base_asset) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])
        if tradifi is not None:
            clauses.append("tradifi = ?")
            params.append(int(tradifi))
        if contract_type:
            clauses.append("contract_type = ?")
            params.append(contract_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type
        )
        if query:
            order = " ORDER BY CASE WHEN symbol = ? THEN 0 ELSE 1 END, tradifi ASC, symbol ASC"
            params.append(query.strip().upper())
        else:
            order = " ORDER BY tradifi ASC, symbol ASC"
        sql = f"SELECT * FROM symbols{where}{order}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        try:
            rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol registry read failed: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_symbol(row) for row in rows)

    def count_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
    ) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type
        )
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM symbols{where}", params).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol registry count failed: {exc}") from exc
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def get_symbol(self, symbol: str) -> SymbolContract | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol lookup failed: {exc}") from exc
        finally:
            connection.close()
        return _row_to_symbol(row) if row is not None else None

    # -- Backfill state --------------------------------------------------

    def start_backfill_run(
        self,
        record: BackfillRunRecord,
        *,
        owner: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Insert a RUNNING run atomically; ``False`` if one is already running.

        A hard crash can leave ``RUNNING`` rows forever. Runs whose heartbeat is
        older than the lease window are marked ``STALE`` before the guard check.
        ``now`` lets the caller inject a ClockPort timestamp.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_dt = (
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                if now is not None
                else datetime.now(UTC)
            )
            stale_before = utc_timestamp(now_dt - timedelta(seconds=STALE_RUNNING_SECONDS))
            connection.execute(
                """
                UPDATE backfill_runs SET
                    status = 'STALE', finished_at = ?, error_code = ?,
                    message = 'stale RUNNING run recovered'
                WHERE status = 'RUNNING'
                  AND COALESCE(updated_at, started_at) < ?
                """,
                (utc_timestamp(now_dt), "INTERRUPTED", stale_before),
            )
            running = connection.execute(
                "SELECT 1 FROM backfill_runs WHERE status = 'RUNNING' LIMIT 1"
            ).fetchone()
            if running is not None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO backfill_runs (
                    run_id, symbol, data_type, start_date, end_date, status,
                    started_at, updated_at, owner, finished_at, estimated_bytes,
                    processed_bytes, files_total, files_verified, files_failed,
                    error_code, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.symbol,
                    record.data_type,
                    record.start_date,
                    record.end_date,
                    record.status,
                    record.started_at,
                    record.started_at,
                    owner,
                    record.finished_at,
                    record.estimated_bytes,
                    record.processed_bytes,
                    record.files_total,
                    record.files_verified,
                    record.files_failed,
                    record.error_code,
                    record.message,
                ),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill run insert failed: {exc}") from exc
        finally:
            connection.close()
        return True

    def touch_backfill_run(self, run_id: str, *, updated_at: str, owner: str | None = None) -> bool:
        """Record a heartbeat; ``False`` means the lease was lost (do not continue)."""
        connection = self._connect()
        try:
            if owner is None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING'",
                    (updated_at, run_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (updated_at, run_id, owner),
                )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def save_backfill_run(self, record: BackfillRunRecord, *, owner: str | None = None) -> bool:
        connection = self._connect()
        try:
            if owner is None:
                cursor = connection.execute(
                    """
                    UPDATE backfill_runs SET
                        status = ?, finished_at = ?, estimated_bytes = ?,
                        processed_bytes = ?, files_total = ?, files_verified = ?,
                        files_failed = ?, error_code = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (
                        record.status,
                        record.finished_at,
                        record.estimated_bytes,
                        record.processed_bytes,
                        record.files_total,
                        record.files_verified,
                        record.files_failed,
                        record.error_code,
                        record.message,
                        record.run_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE backfill_runs SET
                        status = ?, finished_at = ?, estimated_bytes = ?,
                        processed_bytes = ?, files_total = ?, files_verified = ?,
                        files_failed = ?, error_code = ?, message = ?
                    WHERE run_id = ? AND status = 'RUNNING' AND owner = ?
                    """,
                    (
                        record.status,
                        record.finished_at,
                        record.estimated_bytes,
                        record.processed_bytes,
                        record.files_total,
                        record.files_verified,
                        record.files_failed,
                        record.error_code,
                        record.message,
                        record.run_id,
                        owner,
                    ),
                )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def get_backfill_run(self, run_id: str) -> BackfillRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_run(row) if row is not None else None

    def latest_backfill_run(self) -> BackfillRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_run(row) if row is not None else None

    def list_backfill_runs(self, limit: int = 50) -> tuple[BackfillRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM backfill_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_backfill_run(row) for row in rows)

    def upsert_backfill_manifest(self, row: BackfillManifestRow) -> None:
        with closing(self._connect()) as connection, connection:
            self._upsert_backfill_manifest_row(connection, row)

    def _upsert_backfill_manifest_row(
        self, connection: sqlite3.Connection, row: BackfillManifestRow
    ) -> None:
        connection.execute(
            """
            INSERT INTO backfill_manifest (
                file_key, run_id, data_type, symbol, date, source_url, checksum,
                checksum_verified, status, bytes, rows, downloaded_at, verified_at,
                error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_key) DO UPDATE SET
                run_id = excluded.run_id,
                source_url = excluded.source_url,
                checksum = excluded.checksum,
                checksum_verified = excluded.checksum_verified,
                status = excluded.status,
                bytes = excluded.bytes,
                rows = excluded.rows,
                downloaded_at = excluded.downloaded_at,
                verified_at = excluded.verified_at,
                error_code = excluded.error_code
            """,
            (
                row.file_key,
                row.run_id,
                row.data_type,
                row.symbol,
                row.date,
                row.source_url,
                row.checksum,
                int(row.checksum_verified),
                row.status,
                row.bytes,
                row.rows,
                row.downloaded_at,
                row.verified_at,
                row.error_code,
            ),
        )

    def _insert_part_manifest(self, connection: sqlite3.Connection, entry: ManifestEntry) -> None:
        connection.execute(
            """
            INSERT INTO manifest (
                path, data_type, symbol, date, rows, bytes, checksum, source,
                created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                rows = excluded.rows,
                bytes = excluded.bytes,
                checksum = excluded.checksum,
                source = excluded.source,
                status = excluded.status
            """,
            (
                entry.path,
                entry.data_type,
                entry.symbol,
                entry.date,
                entry.rows,
                entry.bytes,
                entry.checksum,
                entry.source,
                entry.created_at,
                entry.status,
            ),
        )

    def _resolve_gaps_for_day(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        data_type: str,
        day: str,
        resolved_at: str,
        resolution: str = "BACKFILL_RESOLVED",
    ) -> None:
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        connection.execute(
            """
            UPDATE data_gaps SET resolved_at = ?, resolution = ?
            WHERE symbol = ? AND data_type = ?
              AND gap_start >= ? AND gap_start < ?
              AND resolved_at IS NULL
            """,
            (
                resolved_at,
                resolution,
                symbol,
                data_type,
                f"{day}T00:00:00.000Z",
                f"{next_day}T00:00:00.000Z",
            ),
        )

    def commit_backfill_file(
        self,
        *,
        row: BackfillManifestRow,
        entries: tuple[ManifestEntry, ...],
        resolved_at: str,
    ) -> None:
        """Unit of work: part manifest + gap resolve + COMPLETE row, COMPLETE last.

        The COMPLETE row must never survive without its published parts; a
        failure rolls the whole transaction back so the caller can delete the
        parts and retry safely.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for entry in entries:
                self._insert_part_manifest(connection, entry)
            self._resolve_gaps_for_day(
                connection,
                symbol=row.symbol,
                data_type=row.data_type,
                day=row.date,
                resolved_at=resolved_at,
            )
            self._upsert_backfill_manifest_row(connection, row)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill file commit failed: {exc}") from exc
        finally:
            connection.close()

    def discard_backfill_file(
        self,
        *,
        file_key: str,
        entries: tuple[ManifestEntry, ...],
        error_code: str,
    ) -> None:
        """Downgrade a published COMPLETE row and drop its part rows before cleanup."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for entry in entries:
                connection.execute("DELETE FROM manifest WHERE path = ?", (entry.path,))
            connection.execute(
                """
                UPDATE backfill_manifest SET
                    status = 'FAILED', checksum_verified = 0, error_code = ?
                WHERE file_key = ? AND status = 'COMPLETE'
                """,
                (error_code, file_key),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill file discard failed: {exc}") from exc
        finally:
            connection.close()

    def get_backfill_manifest(self, file_key: str) -> BackfillManifestRow | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_manifest WHERE file_key = ?", (file_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_manifest(row) if row is not None else None

    def list_backfill_manifest(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        run_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[BackfillManifestRow, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if data_type is not None:
            clauses.append("data_type = ?")
            params.append(data_type)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if start is not None:
            clauses.append("date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("date <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM backfill_manifest{where} ORDER BY date, file_key", params
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_backfill_manifest(row) for row in rows)

    def upsert_data_gap(self, gap: DataGap) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO data_gaps (
                    gap_id, symbol, data_type, gap_start, gap_end, reason, severity,
                    detected_at, source, resolved_at, resolution, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gap_id) DO UPDATE SET
                    gap_start = excluded.gap_start,
                    gap_end = excluded.gap_end,
                    reason = excluded.reason,
                    severity = excluded.severity,
                    detected_at = excluded.detected_at,
                    source = excluded.source,
                    resolved_at = excluded.resolved_at,
                    resolution = excluded.resolution,
                    run_id = excluded.run_id
                """,
                (
                    gap.gap_id,
                    gap.symbol,
                    gap.data_type,
                    gap.start,
                    gap.end,
                    gap.reason,
                    gap.severity.value,
                    gap.detected_at,
                    gap.source,
                    gap.resolved_at,
                    gap.resolution,
                    gap.run_id,
                ),
            )

    def resolve_data_gaps_for_date(
        self,
        *,
        symbol: str,
        data_type: str,
        day: str,
        resolved_at: str,
        resolution: str = "BACKFILL_RESOLVED",
    ) -> None:
        """Mark gaps covering a now-verified/published day as resolved."""
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE data_gaps SET resolved_at = ?, resolution = ?
                WHERE symbol = ? AND data_type = ?
                  AND gap_start >= ? AND gap_start < ?
                  AND resolved_at IS NULL
                """,
                (
                    resolved_at,
                    resolution,
                    symbol,
                    data_type,
                    f"{day}T00:00:00.000Z",
                    f"{next_day}T00:00:00.000Z",
                ),
            )

    def list_data_gaps(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[DataGap, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if data_type is not None:
            clauses.append("data_type = ?")
            params.append(data_type)
        if start is not None:
            clauses.append("gap_start >= ?")
            params.append(f"{start}T00:00:00.000Z")
        if end is not None:
            # Half-open: the end day itself is included, the next day is not.
            end_exclusive = (datetime.fromisoformat(end).date() + timedelta(days=1)).isoformat()
            clauses.append("gap_start < ?")
            params.append(f"{end_exclusive}T00:00:00.000Z")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM data_gaps{where} ORDER BY gap_start, gap_id", params
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_data_gap(row) for row in rows)
