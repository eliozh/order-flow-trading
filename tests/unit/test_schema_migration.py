"""Schema v1 -> v2 migration adds CHECK constraints to an existing symbols table."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from oft.adapters.sqlite_schema import _TRIGGER_STATUSES
from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.config import Settings
from oft.domain.oms import OrderStatus, PositionState
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

    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


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

    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}
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


_V15_PROMPTS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, 't'), (2, 't'), (3, 't'),
    (4, 't'), (5, 't'), (6, 't'), (7, 't'), (8, 't'), (9, 't'), (10, 't'), (11, 't'),
    (12, 't'), (13, 't'), (14, 't'), (15, 't');
CREATE TABLE prompt_templates (
    prompt_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    active_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO prompt_templates VALUES ('analysis', 'ANALYSIS', 2, 't1', 't2');
CREATE TABLE prompt_versions (
    prompt_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (prompt_id, version)
);
INSERT INTO prompt_versions VALUES ('analysis', 1, 'OLD V1', 'HASH1', 0, 't1');
INSERT INTO prompt_versions VALUES ('analysis', 2, 'CUSTOM V2', 'HASH2', 1, 't2');
CREATE TABLE llm_calls (
    call_id TEXT PRIMARY KEY,
    decision_id TEXT,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_output_hash TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO llm_calls VALUES (
    'CALL-1', 'DEC-1', 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', 'IH',
    1, 'REQ-1', 'OK', 'RH', '{}', 0, NULL, 't1'
);
"""


def test_v15_prompt_and_llm_rows_upgrade_to_v16_and_seed_only_missing_families(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V15_PROMPTS)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent re-initialize

    connection = sqlite3.connect(database_path(settings))
    try:
        prompt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(prompt_versions)")
        }
        llm_columns = {row[1] for row in connection.execute("PRAGMA table_info(llm_calls)")}
        preserved = connection.execute(
            "SELECT version, body, body_hash, is_active FROM prompt_versions "
            "WHERE prompt_id = 'analysis' ORDER BY version"
        ).fetchall()
        upgraded_defaults = connection.execute(
            "SELECT system_prompt_id, system_prompt_version, system_body, "
            "system_body_hash, status, author, reason, is_default "
            "FROM prompt_versions WHERE prompt_id = 'analysis' AND version = 2"
        ).fetchone()
        active_version = connection.execute(
            "SELECT active_version FROM prompt_templates WHERE prompt_id = 'analysis'"
        ).fetchone()[0]
        templates = {
            row[0]
            for row in connection.execute("SELECT prompt_id FROM prompt_templates ORDER BY 1")
        }
        llm_defaults = connection.execute(
            "SELECT system_prompt_id, system_prompt_version, system_prompt_hash "
            "FROM llm_calls WHERE call_id = 'CALL-1'"
        ).fetchone()
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {
        "system_prompt_id",
        "system_prompt_version",
        "system_body",
        "system_body_hash",
        "required_variables_json",
        "knowledge_artifact_hash",
        "status",
        "author",
        "reason",
        "is_default",
    } <= prompt_columns
    assert {"system_prompt_id", "system_prompt_version", "system_prompt_hash"} <= llm_columns
    # Operator-authored rows survive untouched.
    assert [tuple(row) for row in preserved] == [
        (1, "OLD V1", "HASH1", 0),
        (2, "CUSTOM V2", "HASH2", 1),
    ]
    assert tuple(upgraded_defaults) == ("", 1, "", "", "ACTIVE", "", "", 0)
    assert active_version == 2
    # Only the missing families are seeded; the existing 'analysis' family and
    # its operator version are not overwritten.
    assert templates == {
        "analysis",
        "analysis:system",
        "entry_recheck",
        "entry_recheck:system",
    }
    assert tuple(llm_defaults) == ("", 1, "")
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


def test_prompt_versions_are_immutable_and_have_one_active_row_per_prompt(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE prompt_versions SET body = 'TAMPERED' "
                "WHERE prompt_id = 'analysis' AND version = 1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE prompt_versions SET body_hash = 'TAMPERED' "
                "WHERE prompt_id = 'analysis' AND version = 1"
            )
        # The whole pinned-system and validation contract is immutable too.
        for column, value in (
            ("system_prompt_id", "'other:system'"),
            ("system_prompt_version", "99"),
            ("system_body", "'TAMPERED'"),
            ("system_body_hash", "'TAMPERED'"),
            ("required_variables_json", "'[]'"),
            ("knowledge_artifact_hash", "'TAMPERED'"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE prompt_versions SET {column} = {value} "
                    "WHERE prompt_id = 'analysis' AND version = 1"
                )
        # Only one row per prompt may be active.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO prompt_versions "
                "(prompt_id, version, body, body_hash, is_active, created_at, status) "
                "VALUES ('analysis', 99, 'BODY', 'HASH', 1, 't', 'ACTIVE')"
            )
        # Activation bookkeeping and drafts stay mutable.
        connection.execute(
            "UPDATE prompt_versions SET is_active = 0, status = 'SUPERSEDED' "
            "WHERE prompt_id = 'analysis' AND version = 1"
        )
        connection.execute(
            "UPDATE prompt_versions SET is_active = 0, status = 'DRAFT' "
            "WHERE prompt_id = 'analysis' AND version = 1"
        )
        # The status enum and the is_active/status invariant are enforced.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO prompt_versions "
                "(prompt_id, version, body, body_hash, is_active, created_at, status) "
                "VALUES ('analysis', 100, 'BODY', 'HASH', 0, 't', 'BOGUS')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO prompt_versions "
                "(prompt_id, version, body, body_hash, is_active, created_at, status) "
                "VALUES ('analysis', 101, 'BODY', 'HASH', 0, 't', 'ACTIVE')"
            )
        connection.commit()
    finally:
        connection.close()


_V16_BUDGET = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at)
    VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),
           (8,'t'),(9,'t'),(10,'t'),(11,'t'),(12,'t'),(13,'t'),(14,'t'),
           (15,'t'),(16,'t');
CREATE TABLE llm_calls (
    call_id TEXT PRIMARY KEY,
    decision_id TEXT,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_output_hash TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO llm_calls VALUES (
    'CALL-16', NULL, 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', 'IH',
    1, 'REQ-16', 'OK', 'RH', '{}', 0, NULL, 't'
);
CREATE TABLE budget_ledger (
    record_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE REFERENCES llm_calls(call_id),
    decision_id TEXT,
    occurred_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd TEXT NOT NULL,
    price_version TEXT NOT NULL,
    unpriced INTEGER NOT NULL CHECK (unpriced IN (0, 1)),
    budget_state TEXT NOT NULL,
    period_day TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
INSERT INTO budget_ledger VALUES (
    'BUD-16', 'CALL-16', NULL, '2026-11-04T09:30:00.000Z', 'stub', 'fixture',
    '0', 'none/v1', 1, 'OK', '2026-11-04', '{}'
);
"""


def test_v16_database_gains_budget_periods_settings_and_provider_capabilities(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V16_BUDGET)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(budget_ledger)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        backfilled = connection.execute(
            "SELECT period_day, period_week, period_month, role "
            "FROM budget_ledger WHERE record_id = 'BUD-16'"
        ).fetchone()
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {"period_week", "period_month", "role"} <= columns
    assert {"budget_settings", "provider_capabilities"} <= tables
    assert {"budget_ledger_week", "budget_ledger_month"} <= indexes
    # 2026-11-04 is ISO week 2026-W45.
    assert tuple(backfilled) == ("2026-11-04", "2026-W45", "2026-11", "")
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}


def _v16_budget_connection(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    connection.executescript(_V16_BUDGET)
    connection.commit()
    return connection


def test_pre_existing_empty_columns_are_backfilled(tmp_path: Path) -> None:
    """A partially migrated v16 DB still gains its week/month buckets (M5)."""
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    connection = _v16_budget_connection(settings)
    try:
        connection.execute(
            "ALTER TABLE budget_ledger ADD COLUMN period_week TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE budget_ledger ADD COLUMN period_month TEXT NOT NULL DEFAULT ''"
        )
        connection.execute("ALTER TABLE budget_ledger ADD COLUMN role TEXT NOT NULL DEFAULT ''")
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        row = connection.execute(
            "SELECT period_week, period_month FROM budget_ledger WHERE record_id = 'BUD-16'"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("2026-W45", "2026-11")


def test_a_malformed_occurred_at_does_not_abort_the_migration(tmp_path: Path) -> None:
    """One unparsable legacy row keeps its defaults instead of blocking boot (M6)."""
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    connection = _v16_budget_connection(settings)
    try:
        connection.execute(
            "INSERT INTO llm_calls VALUES "
            "('CALL-BAD', NULL, 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', "
            "'IH', 1, 'REQ-BAD', 'OK', 'RH', '{}', 0, NULL, 't')"
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES "
            "('BUD-BAD', 'CALL-BAD', NULL, 'not-a-timestamp', 'stub', 'fixture', "
            "'0', 'none/v1', 1, 'OK', '2026-11-05', '{}')"
        )
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()  # must not raise

    connection = sqlite3.connect(database_path(settings))
    try:
        good = connection.execute(
            "SELECT period_week, period_month FROM budget_ledger WHERE record_id = 'BUD-16'"
        ).fetchone()
        bad = connection.execute(
            "SELECT period_week, period_month, period_day "
            "FROM budget_ledger WHERE record_id = 'BUD-BAD'"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(good) == ("2026-W45", "2026-11")
    assert tuple(bad) == ("", "", "2026-11-05")


def test_migration_backfills_utc_boundary_days(tmp_path: Path) -> None:
    """A non-UTC offset buckets by UTC day/week (M4)."""
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    connection = _v16_budget_connection(settings)
    try:
        connection.execute(
            "INSERT INTO llm_calls VALUES "
            "('CALL-UTC', NULL, 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', "
            "'IH', 1, 'REQ-UTC', 'OK', 'RH', '{}', 0, NULL, 't')"
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES "
            "('BUD-UTC', 'CALL-UTC', NULL, '2026-11-08T23:30:00-05:00', 'stub', "
            "'fixture', '0', 'none/v1', 1, 'OK', '', '{}')"
        )
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        row = connection.execute(
            "SELECT period_day, period_week, period_month "
            "FROM budget_ledger WHERE record_id = 'BUD-UTC'"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("2026-11-09", "2026-W46", "2026-11")


_V18_TRIGGERS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at)
    VALUES (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),(8,'t'),
           (9,'t'),(10,'t'),(11,'t'),(12,'t'),(13,'t'),(14,'t'),(15,'t'),
           (16,'t'),(17,'t'),(18,'t');
CREATE TABLE trigger_events (
    trigger_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    triggered_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
INSERT INTO trigger_events VALUES (
    'TRG-V18', 'OPP-V18', 'BTCUSDT', 'PENDING_RECHECK', 'TRG-OPP-V18-KEY',
    '2026-11-09T10:00:00.000Z',
    '{"snapshot": {"market_snapshot_id": "SNAP-V18",
      "snapshot_hash": "HASH-V18", "as_of": "2026-11-09T10:00:00.000Z",
      "data_quality": "PARTIAL"}, "condition_results": [{}, {}]}'
);
INSERT INTO trigger_events VALUES (
    'TRG-BAD', 'OPP-BAD', 'BTCUSDT', 'PENDING_RECHECK', 'TRG-OPP-BAD-KEY',
    '2026-11-09T10:00:00.000Z', 'not-json'
);
"""


def test_v18_trigger_events_gain_snapshot_provenance_and_backfill(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V18_TRIGGERS)
        connection.commit()
    finally:
        connection.close()

    store = SqliteStore(settings)
    with caplog.at_level(logging.WARNING, logger="oft.adapters.sqlite_store"):
        store.initialize()
    store.initialize()  # idempotent

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trigger_events)")}
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'trigger_events'"
            )
        }
        good = connection.execute(
            "SELECT snapshot_hash, as_of, data_quality, condition_count "
            "FROM trigger_events WHERE trigger_id = 'TRG-V18'"
        ).fetchone()
        bad = connection.execute(
            "SELECT snapshot_hash, as_of, data_quality, condition_count "
            "FROM trigger_events WHERE trigger_id = 'TRG-BAD'"
        ).fetchone()
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    finally:
        connection.close()

    assert {"snapshot_hash", "as_of", "data_quality", "condition_count"} <= columns
    assert "trigger_events_opportunity_triggered" in indexes
    assert tuple(good) == ("HASH-V18", "2026-11-09T10:00:00.000Z", "PARTIAL", 2)
    # A malformed legacy payload is skipped with its defaults, never aborting boot.
    assert tuple(bad) == ("", "", "", 0)
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}
    skipped = [
        record
        for record in caplog.records
        if record.getMessage() == "trigger_event_backfill_skipped"
    ]
    assert [record.trigger_id for record in skipped] == ["TRG-BAD"]


def test_concurrent_initializes_upgrade_v19_idempotently(tmp_path: Path) -> None:
    """Concurrent bootstraps must neither race the WAL switch nor the v19 DDL.

    A barrier starts all workers at once so the WAL switch, the trigger
    drop/create pair, and the v19 BEGIN IMMEDIATE upgrade are genuinely
    contended. No test-side retry or sleep is used: the initializer itself must
    serialize the work, so a failure is a real regression rather than luck.
    """
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V18_TRIGGERS)
        connection.commit()
    finally:
        connection.close()

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    errors: list[BaseException] = []

    def boot() -> None:
        barrier.wait(timeout=30)
        try:
            SqliteStore(settings).initialize()
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(lambda _index: boot(), range(worker_count)))

    assert errors == []
    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trigger_events)")}
        good = connection.execute(
            "SELECT snapshot_hash, as_of, data_quality, condition_count "
            "FROM trigger_events WHERE trigger_id = 'TRG-V18'"
        ).fetchone()
        versions = {
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 19"
            )
        }
        highest_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        immutable_triggers = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'prompt_versions_immutable'"
        ).fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert {"snapshot_hash", "as_of", "data_quality", "condition_count"} <= columns
    assert tuple(good) == ("HASH-V18", "2026-11-09T10:00:00.000Z", "PARTIAL", 2)
    # Exactly one recorded v19 upgrade; v20 and v21 are recorded on the same boot.
    assert versions == {19}
    assert highest_version == 21
    assert immutable_triggers == 1
    assert journal_mode.lower() == "wal"


def test_replaying_the_v18_backfill_preserves_evaluation_columns(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    payload = {
        "opportunity_id": "OPP-18",
        "trigger_conditions": [
            {
                "condition_id": "C-1",
                "type": "PRICE_TOUCH_ZONE",
                "params": {"price_low": "69900.00", "price_high": "70100.00"},
                "window": {
                    "start": "2026-11-01T00:00:00.000Z",
                    "end": "2026-12-01T00:00:00.000Z",
                },
                "required_data": ["MARK_PRICE"],
                "evaluable": True,
            }
        ],
        "invalidation_conditions": [],
    }
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                "OPP-18",
                "BTCUSDT",
                "OPEN",
                "2026-11-06T12:00:00.000Z",
                "2026-11-06T12:00:00.000Z",
                json.dumps(payload),
            ),
        )
        connection.execute(
            "INSERT INTO opportunity_conditions "
            "(condition_id, opportunity_id, kind, seq, type, params_json, "
            "window_start, window_end, required_data_json, evaluable, "
            "unavailable_reason, last_value, last_evaluated_at, last_result) "
            "VALUES ('C-1', 'OPP-18', 'TRIGGER', 1, 'PRICE_TOUCH_ZONE', '{}', "
            "'2026-11-01T00:00:00.000Z', '2026-12-01T00:00:00.000Z', '[]', 1, "
            "NULL, '70000.00', '2026-11-06T12:00:00.000Z', 1)"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        row = connection.execute(
            "SELECT last_value, last_evaluated_at, last_result "
            "FROM opportunity_conditions WHERE opportunity_id = 'OPP-18' AND condition_id = 'C-1'"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("70000.00", "2026-11-06T12:00:00.000Z", 1)


def test_v20_orders_bind_to_their_trigger_and_index_the_ledger(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        order_columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
        verdict_columns = {row[1] for row in connection.execute("PRAGMA table_info(risk_verdicts)")}
        order_indexes = {row[1] for row in connection.execute("PRAGMA index_list(orders)")}
        verdict_indexes = {row[1] for row in connection.execute("PRAGMA index_list(risk_verdicts)")}
        position_indexes = {row[1] for row in connection.execute("PRAGMA index_list(positions)")}
    finally:
        connection.close()

    assert {"trigger_id", "decision_id", "purpose"} <= order_columns
    assert {"spread_bps", "expected_slippage_bps", "daily_loss_usd"} <= verdict_columns
    assert "orders_one_entry_per_trigger" in order_indexes
    assert "risk_verdicts_trigger_id" in verdict_indexes
    assert "positions_account_id" in position_indexes


def test_v19_orders_survive_the_v20_upgrade_with_a_null_trigger_id(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
            "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
            "trigger_id, decision_id, purpose) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'ENTRY')",
            (
                "ORD-LEGACY",
                "client-legacy",
                "PAPER",
                "BTCUSDT",
                "FILLED",
                "RISK-LEGACY",
                "idem-legacy",
                "2026-11-01T00:00:00.000Z",
                "2026-11-01T00:00:00.000Z",
                "{}",
            ),
        )
        # Roll the recorded schema back to v19 and remove the v20 additions.
        connection.execute("DROP INDEX IF EXISTS orders_one_entry_per_trigger")
        connection.execute("DROP INDEX IF EXISTS risk_verdicts_trigger_id")
        connection.execute("DROP INDEX IF EXISTS positions_account_id")
        connection.execute("ALTER TABLE orders DROP COLUMN trigger_id")
        connection.execute("ALTER TABLE orders DROP COLUMN decision_id")
        connection.execute("ALTER TABLE orders DROP COLUMN purpose")
        connection.execute("ALTER TABLE risk_verdicts DROP COLUMN spread_bps")
        connection.execute("ALTER TABLE risk_verdicts DROP COLUMN expected_slippage_bps")
        connection.execute("ALTER TABLE risk_verdicts DROP COLUMN daily_loss_usd")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (20, 21)")
        connection.commit()
    finally:
        connection.close()

    SqliteStore(settings).initialize()

    connection = sqlite3.connect(database_path(settings))
    try:
        row = connection.execute(
            "SELECT order_id, trigger_id, purpose FROM orders WHERE order_id = 'ORD-LEGACY'"
        ).fetchone()
        indexes = {item[1] for item in connection.execute("PRAGMA index_list(orders)")}
    finally:
        connection.close()
    assert tuple(row) == ("ORD-LEGACY", None, "ENTRY")
    assert "orders_one_entry_per_trigger" in indexes


# -- v20 -> v21: residual foreign keys, enum CHECKs, and a populated upgrade --

_V20_POPULATED = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES
    (1,'t'),(2,'t'),(3,'t'),(4,'t'),(5,'t'),(6,'t'),(7,'t'),(8,'t'),(9,'t'),(10,'t'),
    (11,'t'),(12,'t'),(13,'t'),(14,'t'),(15,'t'),(16,'t'),(17,'t'),(18,'t'),(19,'t'),(20,'t');
CREATE TABLE opportunities (
    opportunity_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('OPEN','TRIGGERED','EXPIRED','INVALIDATED')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE opportunity_events (
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, seq)
);
CREATE TABLE opportunity_conditions (
    condition_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    kind TEXT NOT NULL CHECK (kind IN ('TRIGGER','INVALIDATION')),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    required_data_json TEXT NOT NULL,
    evaluable INTEGER NOT NULL CHECK (evaluable IN (0, 1)),
    unavailable_reason TEXT,
    last_value TEXT,
    last_evaluated_at TEXT,
    last_result INTEGER CHECK (last_result IN (0, 1) OR last_result IS NULL),
    PRIMARY KEY (opportunity_id, condition_id)
);
CREATE TABLE trigger_events (
    trigger_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    triggered_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL DEFAULT '',
    data_quality TEXT NOT NULL DEFAULT '',
    condition_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    raw_output_hash TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE llm_calls (
    call_id TEXT PRIMARY KEY,
    decision_id TEXT,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_output_hash TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    system_prompt_id TEXT NOT NULL DEFAULT '',
    system_prompt_version INTEGER NOT NULL DEFAULT 1,
    system_prompt_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE budget_ledger (
    record_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE REFERENCES llm_calls(call_id),
    decision_id TEXT,
    occurred_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd TEXT NOT NULL,
    price_version TEXT NOT NULL,
    unpriced INTEGER NOT NULL CHECK (unpriced IN (0, 1)),
    budget_state TEXT NOT NULL,
    period_day TEXT NOT NULL,
    period_week TEXT NOT NULL DEFAULT '',
    period_month TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL
);
CREATE TABLE risk_verdicts (
    risk_verdict_id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL REFERENCES trigger_events(trigger_id),
    decision_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('ALLOW','REDUCE','BLOCK')),
    binding_cap TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    spread_bps REAL,
    expected_slippage_bps REAL,
    daily_loss_usd TEXT
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_verdict_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trigger_id TEXT,
    decision_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'ENTRY'
);
CREATE UNIQUE INDEX orders_one_entry_per_trigger
    ON orders(trigger_id, environment, purpose)
    WHERE trigger_id IS NOT NULL AND purpose = 'ENTRY';
CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX fills_order_id ON fills(order_id);
CREATE TABLE positions (
    position_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX positions_state ON positions(state);
CREATE INDEX positions_account_id ON positions(account_id);
CREATE TABLE paper_accounts (
    account_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    currency TEXT NOT NULL,
    initial_balance TEXT NOT NULL,
    balance TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO opportunities VALUES (
    'OPP-1', 'BTCUSDT', 'TRIGGERED', '2026-11-10T00:00:00.000Z',
    '2026-11-10T00:00:00.000Z',
    '{"opportunity_id": "OPP-1", "trigger_conditions": [], "invalidation_conditions": []}',
    '2026-12-10T00:00:00.000Z'
);
INSERT INTO opportunity_events VALUES (
    'OPP-1', 1, '2026-11-10T00:00:00.000Z', 'OPEN', 'TRIGGERED',
    'TRIGGERED', 'trigger fired'
);
INSERT INTO opportunity_conditions VALUES (
    'C-1', 'OPP-1', 'TRIGGER', 1, 'PRICE_TOUCH_ZONE', '{}',
    '2026-11-01T00:00:00.000Z', '2026-12-01T00:00:00.000Z', '[]', 1, NULL,
    '70000.00', '2026-11-10T00:00:00.000Z', 1
);
INSERT INTO trigger_events VALUES (
    'TRG-1', 'OPP-1', 'BTCUSDT', 'PENDING_RECHECK', 'TRG-KEY-1',
    '2026-11-10T00:00:00.000Z', '{"status": "PENDING_RECHECK"}',
    'SNAP-HASH', '2026-11-10T00:00:00.000Z', 'COMPLETE', 1
);
INSERT INTO decisions VALUES (
    'DEC-1', 'ANALYSIS', 'MARKET_ANALYSIS', 'VALID', 'RAW-HASH',
    '2026-11-10T00:00:00.000Z', '{}'
);
INSERT INTO llm_calls VALUES (
    'CALL-1', 'DEC-1', 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', 'IH',
    1, 'REQ-1', 'OK', 'RH', '{}', 0, NULL, '2026-11-10T00:00:00.000Z', '', 1, ''
);
INSERT INTO risk_verdicts VALUES (
    'RISK-1', 'TRG-1', 'DEC-1', 'ALLOW', 'NONE',
    '2026-11-10T00:00:00.000Z', '2026-11-10T00:01:00.000Z', '{}', NULL, NULL, NULL
);
INSERT INTO orders VALUES (
    'ORD-1', 'client-1', 'PAPER', 'BTCUSDT', 'FILLED', 'RISK-1', 'idem-1',
    '2026-11-10T00:00:00.000Z', '2026-11-10T00:00:00.000Z', '{}',
    'TRG-1', 'DEC-1', 'ENTRY'
);
INSERT INTO fills VALUES (
    'FIL-1', 'ORD-1', 'PAPER', 'BTCUSDT', '2026-11-10T00:00:00.000Z', '{}'
);
INSERT INTO paper_accounts VALUES (
    'ACC-1', 'PAPER', 'USDT', '10000.00', '9990.00', '2026-11-10T00:00:00.000Z'
);
INSERT INTO positions VALUES (
    'POS-1', 'ACC-1', 'PAPER', 'BTCUSDT', 'OPEN',
    '2026-11-10T00:00:00.000Z', '{}'
);
INSERT INTO budget_ledger VALUES (
    'BUD-1', 'CALL-1', 'DEC-1', '2026-11-10T00:00:00.000Z', 'stub', 'fixture',
    '0.0015', 'stub/v1', 1, 'OK', '2026-11-10', '2026-W45', '2026-11', 'ANALYSIS',
    '{}'
);
"""

_V21_REBUILT_TABLES = (
    "llm_calls",
    "decisions",
    "trigger_events",
    "risk_verdicts",
    "orders",
    "positions",
)


def _v20_settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V20_POPULATED)
        connection.commit()
    finally:
        connection.close()
    return settings


def _normalized_ddl(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    assert row is not None and row[0] is not None
    return " ".join(str(row[0]).split())


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}


def _foreign_key_targets(connection: sqlite3.Connection, table: str) -> set[tuple[str, str]]:
    """The ``(child column, parent table)`` pairs of a table's foreign keys."""
    return {(row[3], row[2]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}


def test_populated_v20_database_upgrades_to_v21_preserving_every_row(
    tmp_path: Path,
) -> None:
    settings = _v20_settings(tmp_path)
    before = sqlite3.connect(database_path(settings))
    try:
        from_version = before.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        before.close()
    assert from_version == 20

    store = SqliteStore(settings)
    store.initialize()
    store.initialize()  # idempotent rebuild

    connection = sqlite3.connect(database_path(settings))
    try:
        to_version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "llm_calls",
                "decisions",
                "trigger_events",
                "risk_verdicts",
                "orders",
                "fills",
                "positions",
                "paper_accounts",
                "opportunities",
                "opportunity_conditions",
                "opportunity_events",
                "budget_ledger",
            )
        }
        preserved = {
            "llm_call": connection.execute(
                "SELECT call_id, decision_id, status FROM llm_calls"
            ).fetchone(),
            "decision": connection.execute(
                "SELECT decision_id, validation_status FROM decisions"
            ).fetchone(),
            "trigger": connection.execute(
                "SELECT trigger_id, opportunity_id, status FROM trigger_events"
            ).fetchone(),
            "verdict": connection.execute(
                "SELECT risk_verdict_id, trigger_id, decision_id FROM risk_verdicts"
            ).fetchone(),
            "order": connection.execute(
                "SELECT order_id, risk_verdict_id, trigger_id, decision_id, purpose FROM orders"
            ).fetchone(),
            "fill": connection.execute("SELECT fill_id, order_id FROM fills").fetchone(),
            "position": connection.execute(
                "SELECT position_id, account_id, state FROM positions"
            ).fetchone(),
            "account": connection.execute(
                "SELECT account_id, balance FROM paper_accounts"
            ).fetchone(),
            "opportunity": connection.execute(
                "SELECT opportunity_id, state FROM opportunities"
            ).fetchone(),
            "condition": connection.execute(
                "SELECT condition_id, opportunity_id, last_result FROM opportunity_conditions"
            ).fetchone(),
            "event": connection.execute(
                "SELECT opportunity_id, seq, to_state FROM opportunity_events"
            ).fetchone(),
            "budget": connection.execute(
                "SELECT record_id, call_id, decision_id FROM budget_ledger"
            ).fetchone(),
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert to_version == 21
    assert {20, 21} <= versions
    # Every populated row survives the rebuild: one row in every table.
    assert counts == {
        "llm_calls": 1,
        "decisions": 1,
        "trigger_events": 1,
        "risk_verdicts": 1,
        "orders": 1,
        "fills": 1,
        "positions": 1,
        "paper_accounts": 1,
        "opportunities": 1,
        "opportunity_conditions": 1,
        "opportunity_events": 1,
        "budget_ledger": 1,
    }
    # Columns (and their values) are preserved across the table rebuilds.
    assert tuple(preserved["llm_call"]) == ("CALL-1", "DEC-1", "OK")
    assert tuple(preserved["decision"]) == ("DEC-1", "VALID")
    assert tuple(preserved["trigger"]) == ("TRG-1", "OPP-1", "PENDING_RECHECK")
    assert tuple(preserved["verdict"]) == ("RISK-1", "TRG-1", "DEC-1")
    assert tuple(preserved["order"]) == ("ORD-1", "RISK-1", "TRG-1", "DEC-1", "ENTRY")
    assert tuple(preserved["fill"]) == ("FIL-1", "ORD-1")
    assert tuple(preserved["position"]) == ("POS-1", "ACC-1", "OPEN")
    assert tuple(preserved["account"]) == ("ACC-1", "9990.00")
    assert tuple(preserved["opportunity"]) == ("OPP-1", "TRIGGERED")
    assert tuple(preserved["condition"]) == ("C-1", "OPP-1", 1)
    assert tuple(preserved["event"]) == ("OPP-1", 1, "TRIGGERED")
    assert tuple(preserved["budget"]) == ("BUD-1", "CALL-1", "DEC-1")
    assert foreign_key_errors == []


def test_upgraded_tables_match_a_fresh_database(tmp_path: Path) -> None:
    upgraded_settings = _v20_settings(tmp_path / "upgraded")
    SqliteStore(upgraded_settings).initialize()
    fresh_settings = Settings(env="test", port=8765, data_dir=tmp_path / "fresh" / "data")
    SqliteStore(fresh_settings).initialize()

    upgraded = sqlite3.connect(database_path(upgraded_settings))
    fresh = sqlite3.connect(database_path(fresh_settings))
    try:
        for table in _V21_REBUILT_TABLES:
            assert _normalized_ddl(upgraded, table) == _normalized_ddl(fresh, table), table
            upgraded_fks = upgraded.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            fresh_fks = fresh.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert [tuple(row) for row in upgraded_fks] == [tuple(row) for row in fresh_fks], table
            upgraded_indexes = _index_names(upgraded, table)
            fresh_indexes = _index_names(fresh, table)
            assert upgraded_indexes == fresh_indexes, table
        # The two FK child columns added in v21 must be indexed on both paths.
        for connection in (upgraded, fresh):
            assert "risk_verdicts_decision_id" in _index_names(connection, "risk_verdicts")
            assert "orders_risk_verdict_id" in _index_names(connection, "orders")
            # ``legacy_alter_table`` must keep these non-rebuilt children
            # pointing at the live tables rather than the dropped ``*_v20`` one.
            assert ("order_id", "orders") in _foreign_key_targets(connection, "fills")
            assert ("call_id", "llm_calls") in _foreign_key_targets(connection, "budget_ledger")
    finally:
        upgraded.close()
        fresh.close()


def _v20_settings_with_invalid_trigger_status(tmp_path: Path) -> Settings:
    settings = _v20_settings(tmp_path)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES "
            "('TRG-BAD', 'OPP-1', 'BTCUSDT', 'LEGACY_VALUE', 'TRG-KEY-BAD', 't', '{}')"
        )
        connection.commit()
    finally:
        connection.close()
    return settings


def test_v21_aborts_and_leaves_v20_untouched_on_an_invalid_legacy_row(
    tmp_path: Path,
) -> None:
    settings = _v20_settings_with_invalid_trigger_status(tmp_path)

    with pytest.raises(sqlite3.IntegrityError) as caught:
        SqliteStore(settings).initialize()

    message = str(caught.value)
    assert "trigger_events" in message
    assert "v20" in message

    connection = sqlite3.connect(database_path(settings))
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        rows = connection.execute(
            "SELECT trigger_id, status FROM trigger_events ORDER BY trigger_id"
        ).fetchall()
    finally:
        connection.close()

    assert version == 20
    # The whole rebuild rolled back: no ``*_v20`` scratch table survives and the
    # offending row is preserved verbatim (never defaulted, rewritten, dropped).
    assert not {name for name in tables if name.endswith("_v20")}
    assert [tuple(row) for row in rows] == [
        ("TRG-1", "PENDING_RECHECK"),
        ("TRG-BAD", "LEGACY_VALUE"),
    ]


def _v20_settings_with_orphan_foreign_key(tmp_path: Path) -> Settings:
    """A v20 database whose ``orders`` row points at a missing risk verdict."""
    settings = _v20_settings(tmp_path)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
            "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
            "trigger_id, decision_id, purpose) VALUES "
            "('ORD-ORPHAN', 'client-orphan', 'PAPER', 'BTCUSDT', 'FILLED', 'MISSING', "
            "'idem-orphan', 't', 't', '{}', NULL, NULL, 'ENTRY')"
        )
        connection.commit()
    finally:
        connection.close()
    return settings


def test_v21_aborts_and_leaves_v20_untouched_on_an_orphan_foreign_key(
    tmp_path: Path,
) -> None:
    settings = _v20_settings_with_orphan_foreign_key(tmp_path)

    with pytest.raises(sqlite3.IntegrityError) as caught:
        SqliteStore(settings).initialize()

    message = str(caught.value)
    # The abort names the child table and the missing parent, not just "error".
    assert "orders" in message
    assert "risk_verdicts" in message
    assert "v20" in message

    connection = sqlite3.connect(database_path(settings))
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "llm_calls",
                "decisions",
                "trigger_events",
                "risk_verdicts",
                "orders",
                "fills",
                "positions",
                "paper_accounts",
                "opportunities",
                "opportunity_conditions",
                "opportunity_events",
                "budget_ledger",
            )
        }
        orders = connection.execute(
            "SELECT order_id, risk_verdict_id FROM orders ORDER BY order_id"
        ).fetchall()
    finally:
        connection.close()

    assert version == 20
    # The whole rebuild rolled back: no ``*_v20`` scratch table survives and
    # every row is preserved verbatim, orphan included.
    assert not {name for name in tables if name.endswith("_v20")}
    assert counts == {
        "llm_calls": 1,
        "decisions": 1,
        "trigger_events": 1,
        "risk_verdicts": 1,
        "orders": 2,
        "fills": 1,
        "positions": 1,
        "paper_accounts": 1,
        "opportunities": 1,
        "opportunity_conditions": 1,
        "opportunity_events": 1,
        "budget_ledger": 1,
    }
    assert [tuple(row) for row in orders] == [
        ("ORD-1", "RISK-1"),
        ("ORD-ORPHAN", "MISSING"),
    ]


def _seed_oms_parents(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            "INSERT OR IGNORE INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES ('OPP-1', 'BTCUSDT', 'TRIGGERED', 't', 't', '{}', NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES ('TRG-1', 'OPP-1', 'BTCUSDT', 'PENDING_RECHECK', "
            "'TRG-KEY-1', 't', '{}')"
        )
        connection.execute(
            "INSERT OR IGNORE INTO decisions "
            "(decision_id, role, decision_type, validation_status, raw_output_hash, "
            "persisted_at, payload_json) VALUES "
            "('DEC-1', 'ANALYSIS', 'MARKET_ANALYSIS', 'VALID', 'H', 't', '{}')"
        )
        connection.execute(
            "INSERT OR IGNORE INTO risk_verdicts "
            "(risk_verdict_id, trigger_id, decision_id, verdict, binding_cap, computed_at, "
            "expires_at, payload_json) VALUES "
            "('RISK-1', 'TRG-1', 'DEC-1', 'ALLOW', 'NONE', 't', 't', '{}')"
        )
        connection.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, environment, currency, initial_balance, balance, updated_at) "
            "VALUES ('ACC-1', 'PAPER', 'USDT', '10000.00', '10000.00', 't')"
        )


def _fk_connection(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(settings))
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def test_v21_rejects_orphan_foreign_keys(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _seed_oms_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
                "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
                "trigger_id, decision_id, purpose) VALUES "
                "('ORD-ORPHAN', 'client-orphan', 'PAPER', 'BTCUSDT', 'FILLED', 'MISSING', "
                "'idem-orphan', 't', 't', '{}', NULL, NULL, 'ENTRY')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO risk_verdicts "
                "(risk_verdict_id, trigger_id, decision_id, verdict, binding_cap, computed_at, "
                "expires_at, payload_json) VALUES "
                "('RISK-ORPHAN', 'TRG-1', 'MISSING', 'ALLOW', 'NONE', 't', 't', '{}')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO positions "
                "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
                "VALUES ('POS-ORPHAN', 'MISSING', 'PAPER', 'BTCUSDT', 'OPEN', 't', '{}')"
            )
    finally:
        connection.close()


def _insert_llm_call(connection: sqlite3.Connection, status: str) -> None:
    connection.execute(
        "INSERT INTO llm_calls (call_id, decision_id, role, provider, model, prompt_id, "
        "prompt_version, prompt_hash, input_hash, attempt, request_id, status, "
        "raw_output_hash, usage_json, latency_ms, error_code, created_at) VALUES "
        "(?, NULL, 'ANALYSIS', 'stub', 'fixture', 'analysis', 1, 'PH', 'IH', 1, 'REQ', ?, "
        "'RH', '{}', 0, NULL, 't')",
        (f"CALL-{status}", status),
    )


def test_v21_rejects_an_invalid_value_in_each_enum_column(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _seed_oms_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_llm_call(connection, "BOGUS")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO decisions "
                "(decision_id, role, decision_type, validation_status, raw_output_hash, "
                "persisted_at, payload_json) VALUES "
                "('DEC-BOGUS', 'ANALYSIS', 'MARKET_ANALYSIS', 'BOGUS', 'H', 't', '{}')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO trigger_events "
                "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
                "payload_json) VALUES "
                "('TRG-BOGUS', 'OPP-1', 'BTCUSDT', 'BOGUS', 'TRG-KEY-BOGUS', 't', '{}')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
                "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
                "trigger_id, decision_id, purpose) VALUES "
                "('ORD-BOGUS', 'client-bogus', 'PAPER', 'BTCUSDT', 'BOGUS', 'RISK-1', "
                "'idem-bogus', 't', 't', '{}', NULL, NULL, 'ENTRY')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO positions "
                "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
                "VALUES ('POS-BOGUS', 'ACC-1', 'PAPER', 'BTCUSDT', 'BOGUS', 't', '{}')"
            )
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["OK", "ERROR"])
def test_v21_accepts_every_llm_call_status(tmp_path: Path, status: str) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _insert_llm_call(connection, status)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["VALID", "REPAIRED", "REJECTED"])
def test_v21_accepts_every_decision_validation_status(tmp_path: Path, status: str) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        connection.execute(
            "INSERT INTO decisions "
            "(decision_id, role, decision_type, validation_status, raw_output_hash, "
            "persisted_at, payload_json) VALUES "
            "(?, 'ANALYSIS', 'MARKET_ANALYSIS', ?, 'H', 't', '{}')",
            (f"DEC-{status}", status),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("status", _TRIGGER_STATUSES)
def test_v21_accepts_every_trigger_status(tmp_path: Path, status: str) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _seed_oms_parents(connection)
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES (?, 'OPP-1', 'BTCUSDT', ?, ?, 't', '{}')",
            (f"TRG-{status}", status, f"TRG-KEY-{status}"),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("status", [member.value for member in OrderStatus])
def test_v21_accepts_every_order_status(tmp_path: Path, status: str) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _seed_oms_parents(connection)
        connection.execute(
            "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
            "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
            "trigger_id, decision_id, purpose) VALUES "
            "(?, ?, 'PAPER', 'BTCUSDT', ?, 'RISK-1', ?, 't', 't', '{}', NULL, NULL, 'ENTRY')",
            (f"ORD-{status}", f"client-{status}", status, f"idem-{status}"),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("state", [member.value for member in PositionState])
def test_v21_accepts_every_position_state(tmp_path: Path, state: str) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    connection = _fk_connection(settings)
    try:
        _seed_oms_parents(connection)
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, 'ACC-1', 'PAPER', 'BTCUSDT', ?, 't', '{}')",
            (f"POS-{state}", state),
        )
        connection.commit()
    finally:
        connection.close()


def test_sqlite_store_module_stays_under_the_soft_ceiling() -> None:
    import oft.adapters.sqlite_store as store_module

    source_path = Path(store_module.__file__)
    assert len(source_path.read_text(encoding="utf-8").splitlines()) < 800
