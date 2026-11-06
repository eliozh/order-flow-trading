"""SQLite transactional store (WAL) for scans and the Parquet manifest.

The daemon is the only writer. Diagnostic commands open the database with
``mode=ro`` so ``oft status`` / ``oft doctor`` never block or create files.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from oft.config import Settings
from oft.ports.store import ManifestEntry, ScanRecord

_SCHEMA_VERSION = 1


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "oft.db"


def initialize_database(settings: Settings) -> None:
    """Create the data directory and the walking-skeleton schema."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        with connection:
            connection.executescript(
                """
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
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, "2026-11-06T00:00:00.000Z"),
            )
    finally:
        connection.close()


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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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

    def list_manifest(self) -> tuple[ManifestEntry, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute("SELECT * FROM manifest ORDER BY created_at, path").fetchall()
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
