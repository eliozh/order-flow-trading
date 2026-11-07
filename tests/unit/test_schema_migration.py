"""Schema v1 -> v2 migration adds CHECK constraints to an existing symbols table."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.config import Settings
from oft.ports.store import ScanSymbolRecord

_V1_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-11-06T00:00:00.000Z');
CREATE TABLE symbols (
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
    updated_at TEXT NOT NULL
);
INSERT INTO symbols VALUES (
    'BTCUSDT', 'BTC', 'USDT', 'PERPETUAL', 'COIN', 'TRADING',
    '0.10', '0.001', '0.001', '5.00', 2, 3, 1567900800000, 0,
    '2026-11-06T04:00:00.000Z'
);
"""


def _v1_settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V1_SCHEMA)
        connection.commit()
    finally:
        connection.close()
    return settings


def test_v1_database_upgrades_to_v2_with_effective_checks(tmp_path: Path) -> None:
    settings = _v1_settings(tmp_path)
    store = SqliteStore(settings)

    store.initialize()
    store.initialize()  # idempotent

    assert store.get_symbol("BTCUSDT") is not None
    connection = sqlite3.connect(database_path(settings))
    try:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'symbols'"
        ).fetchone()[0]
        assert "CHECK" in ddl
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO symbols VALUES (
                    'BADUSDT', 'B', 'USDT', 'PERPETUAL', NULL, 'TRADING',
                    '0.1', '0.1', '1', '5', -1, 2, 1567900800000, 0, 't', 0, NULL
                )
                """
            )
    finally:
        connection.close()

    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


_V2_BACKFILL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, 't1');
INSERT INTO schema_migrations (version, applied_at) VALUES (2, 't2');
CREATE TABLE backfill_runs (
    run_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    estimated_bytes INTEGER NOT NULL,
    processed_bytes INTEGER NOT NULL,
    files_total INTEGER NOT NULL,
    files_verified INTEGER NOT NULL,
    files_failed INTEGER NOT NULL,
    error_code TEXT,
    message TEXT
);
INSERT INTO backfill_runs VALUES (
    'BF-1', 'BTCUSDT', 'aggTrades', '2026-10-01', '2026-10-07', 'RUNNING',
    '2026-11-06T00:00:00.000Z', NULL, 0, 0, 7, 0, 0, NULL, NULL
);
"""


def test_v2_database_gains_the_backfill_heartbeat_column(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V2_BACKFILL)
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(backfill_runs)")}
        updated_at = connection.execute(
            "SELECT updated_at FROM backfill_runs WHERE run_id = 'BF-1'"
        ).fetchone()[0]
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert "updated_at" in columns
    assert updated_at == "2026-11-06T00:00:00.000Z"
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


def test_v5_checks_indexes_and_foreign_keys(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = sqlite3.connect(database_path(settings))
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "manifest_symbol_date" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO backfill_manifest (
                    file_key, run_id, data_type, symbol, date, source_url, checksum,
                    checksum_verified, status, bytes, rows
                ) VALUES ('k', NULL, 'aggTrades', 'BTCUSDT', '2026-10-01', 'u', 'c',
                          1, 'BOGUS', 1, 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO backfill_manifest (
                    file_key, run_id, data_type, symbol, date, source_url, checksum,
                    checksum_verified, status, bytes, rows
                ) VALUES ('k2', 'MISSING-RUN', 'aggTrades', 'BTCUSDT', '2026-10-01',
                          'u', 'c', 1, 'COMPLETE', 1, 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO data_gaps (
                    gap_id, symbol, data_type, gap_start, gap_end, reason, severity,
                    detected_at, source
                ) VALUES ('g', 'BTCUSDT', 'aggTrades', 's', 'e', 'r', 'BOGUS', 't', 'BACKFILL')
                """
            )
    finally:
        connection.close()


def test_v5_database_migrates_manifest_metrics_columns(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
                VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t');
            CREATE TABLE manifest (
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
            INSERT INTO manifest VALUES (
                '/tmp/legacy.parquet','agg_trades','BTCUSDT','2026-10-01',
                1,1,'c','archive:x','2026-11-06T00:00:00.000Z','PUBLISHED'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(manifest)")}
        legacy = connection.execute(
            "SELECT path, metric_version, window_start FROM manifest "
            "WHERE path = '/tmp/legacy.parquet'"
        ).fetchone()
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {"metric_version", "input_manifest_hash", "window_start", "window_end"} <= columns
    assert legacy == ("/tmp/legacy.parquet", None, None)
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


def test_v6_database_migrates_starred_columns(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
                VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t');
            CREATE TABLE symbols (
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
                updated_at TEXT NOT NULL
            );
            INSERT INTO symbols VALUES (
                'BTCUSDT','BTC','USDT','PERPETUAL','COIN','TRADING',
                '0.10','0.001','0.001','5.00',2,3,1567900800000,0,'t'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(symbols)")}
        row = connection.execute(
            "SELECT symbol, starred, starred_at FROM symbols WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        versions = {row_[0] for row_ in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {"starred", "starred_at"} <= columns
    assert row == ("BTCUSDT", 0, None)
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


def test_manifest_run_metadata_columns_are_added_idempotently(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    store.initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(manifest)")}
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {"run_id", "last_n", "recomputed_from", "parent_run_id"} <= columns
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


_V12_JOB_RUNS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at)
    VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),
           (8,'t'),(9,'t'),(10,'t'),(11,'t'),(12,'t');
CREATE TABLE job_runs (
    run_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    message TEXT,
    lease_owner TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'backfill',
    metrics_only INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    UNIQUE (job_type, window_start)
);
INSERT INTO job_runs (
    run_id, job_type, window_start, status, trigger_source, started_at,
    finished_at, error_code, message, lease_owner, lease_expires_at, heartbeat_at,
    source, metrics_only, provider
) VALUES (
    'JOB-1', 'scan', '2026-11-07T08:00:00.000Z', 'DONE', 'SCHEDULED', 't',
    't', NULL, NULL, 'owner', 't', 't', 'backfill', 0, NULL
);
"""


def test_v12_database_gains_the_job_run_scope_symbol_column(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V12_JOB_RUNS)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(job_runs)")}
        symbol = connection.execute(
            "SELECT symbol FROM job_runs WHERE run_id = 'JOB-1'"
        ).fetchone()[0]
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert "symbol" in columns
    assert symbol is None  # pre-v13 batch rows keep a null (batch) scope
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}


_V13_JOB_RUNS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at)
    VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),
           (8,'t'),(9,'t'),(10,'t'),(11,'t'),(12,'t'),(13,'t');
CREATE TABLE job_runs (
    run_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('RUNNING','DONE','FAILED','MISSED','SKIPPED')
    ),
    trigger_source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    message TEXT,
    lease_owner TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'backfill',
    metrics_only INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    symbol TEXT,
    UNIQUE (job_type, window_start)
);
INSERT INTO job_runs (
    run_id, job_type, window_start, status, trigger_source, started_at,
    finished_at, error_code, message, lease_owner, lease_expires_at, heartbeat_at,
    source, metrics_only, provider, symbol
) VALUES (
    'JOB-13', 'scan', '2026-11-07T08:00:00.000Z', 'DONE', 'MANUAL', 't',
    't', NULL, NULL, 'owner', 't', 't', 'live', 1, 'stub', 'BTCUSDT'
);
"""


def test_v13_database_gains_period_and_the_partial_status(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V13_JOB_RUNS)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent rebuild

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(job_runs)")}
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_runs'"
        ).fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'job_runs'"
            )
        }
        preserved = connection.execute(
            "SELECT status, source, metrics_only, provider, symbol, period "
            "FROM job_runs WHERE run_id = 'JOB-13'"
        ).fetchone()
        connection.execute("UPDATE job_runs SET status = 'PARTIAL' WHERE run_id = 'JOB-13'")
    finally:
        connection.close()

    assert "period" in columns
    assert "'PARTIAL'" in definition
    assert "job_runs_type_heartbeat" in indexes
    # The populated row survives with its options and a null period default.
    assert tuple(preserved) == ("DONE", "live", 1, "stub", "BTCUSDT", None)


_V10_SCAN_SYMBOLS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at)
    VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),
           (8,'t'),(9,'t'),(10,'t');
CREATE TABLE scan_symbols (
    scan_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN ('QUEUED','DATA','METRICS','REPORT','DONE','FAILED')
    ),
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    report_id TEXT,
    data_quality TEXT,
    PRIMARY KEY (scan_id, symbol)
);
INSERT INTO scan_symbols VALUES (
    'SCAN-1', 'BTCUSDT', 'DONE', 't', 't', NULL, 'RPT-1', 'COMPLETE'
);
"""


_V9_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (2, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (3, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (4, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (5, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (6, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (7, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (8, 't');
INSERT INTO schema_migrations (version, applied_at) VALUES (9, 't');
CREATE TABLE scans (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reports (
    report_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
"""


def test_v10_scan_symbols_upgrade_preserves_rows_and_accepts_llm_costs(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V10_SCAN_SYMBOLS)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent rebuild

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_symbols)")}
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'scan_symbols'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert {
        "error_message",
        "failed_stage",
        "llm_status",
        "llm_tokens",
        "cost_usd",
        "cost_unpriced",
    } <= columns
    assert "'LLM'" in definition and "'DEGRADED'" in definition

    preserved = {item.symbol: item for item in store.list_scan_symbols("SCAN-1")}
    assert set(preserved) == {"BTCUSDT"}
    assert preserved["BTCUSDT"].stage == "DONE"
    assert preserved["BTCUSDT"].report_id == "RPT-1"
    assert preserved["BTCUSDT"].llm_tokens == 0
    assert preserved["BTCUSDT"].cost_usd == "0"
    assert preserved["BTCUSDT"].cost_unpriced is True

    store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id="SCAN-1",
            symbol="ETHUSDT",
            stage="DEGRADED",
            started_at="t",
            finished_at="t",
            error_code=None,
            report_id="RPT-2",
            data_quality="COMPLETE",
            llm_status="REJECTED",
            llm_tokens=150,
            cost_usd="0.0015",
            cost_unpriced=False,
        )
    )
    store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id="SCAN-1",
            symbol="SOLUSDT",
            stage="LLM",
            started_at="t",
            finished_at=None,
            error_code=None,
            report_id=None,
            data_quality=None,
            llm_status=None,
            llm_tokens=0,
            cost_usd="0",
            cost_unpriced=True,
        )
    )

    upgraded = {item.symbol: item for item in store.list_scan_symbols("SCAN-1")}
    assert upgraded["ETHUSDT"].stage == "DEGRADED"
    assert upgraded["ETHUSDT"].llm_status == "REJECTED"
    assert upgraded["ETHUSDT"].llm_tokens == 150
    assert upgraded["ETHUSDT"].cost_usd == "0.0015"
    assert upgraded["ETHUSDT"].cost_unpriced is False
    assert upgraded["SOLUSDT"].stage == "LLM"


def test_v9_database_upgrades_to_v10_with_ledgers_and_indexes(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V9_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        report_columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
    assert "llm_json" in report_columns
    assert {
        "llm_calls",
        "decisions",
        "budget_ledger",
        "opportunities",
        "opportunity_events",
        "trigger_events",
        "risk_verdicts",
        "orders",
        "fills",
        "positions",
        "paper_accounts",
        "audit_events",
    } <= tables
    assert {
        "fills_order_id",
        "opportunities_state_created",
        "trigger_events_triggered_at",
    } <= indexes
    assert foreign_key_errors == []
