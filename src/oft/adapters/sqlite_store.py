"""SQLite transactional store (WAL) for scans and the Parquet manifest.

The daemon is the only writer. Diagnostic commands open the database with
``mode=ro`` so ``oft status`` / ``oft doctor`` never block or create files.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from oft.config import Settings
from oft.domain.audit import build_audit_entry
from oft.domain.budget_periods import budget_record_id, period_parts
from oft.domain.data_quality import DataGap, GapSeverity
from oft.domain.prompts import (
    DEFAULT_SYSTEM_BODIES,
    DEFAULT_USER_BODIES,
    PROMPT_IDS,
    REQUIRED_VARIABLES,
    SYSTEM_PROMPT_IDS,
    PromptRole,
    PromptStatus,
    content_hash,
)
from oft.domain.symbols import SymbolContract
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    AuditEventDraft,
    AuditEventRecord,
    BackfillManifestRow,
    BackfillRunRecord,
    BudgetRecord,
    BudgetRecordConflictError,
    BudgetSettingsRecord,
    DecisionRecord,
    FillRecord,
    IdempotencyRecord,
    JobLockRecord,
    JobRunRecord,
    LeaseLostError,
    LLMCallRecord,
    ManifestEntry,
    OmsCommit,
    OpportunityEventRecord,
    OpportunityRecord,
    OrderRecord,
    PaperAccountRecord,
    PositionRecord,
    PromptTemplateRecord,
    PromptVersionRecord,
    ProviderCapabilityRecord,
    ReportRecord,
    RiskVerdictRecord,
    ScanRecord,
    ScanSymbolRecord,
    StoreUnavailableError,
    SymbolUpsertResult,
    TriggerEventRecord,
)

_LOGGER = logging.getLogger(__name__)


def _zero_budget_totals() -> dict[str, dict[str, Any]]:
    return {
        "day": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
        "week": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
        "month": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
    }


_SCHEMA_VERSION = 17
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

_JOB_RUNS_DEFINITION = """
    run_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('RUNNING','PARTIAL','DONE','FAILED','MISSED','SKIPPED')
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
    period TEXT,
    UNIQUE (job_type, window_start)
"""

_JOB_LOCKS_DEFINITION = """
    lock_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
"""

_SCAN_SYMBOLS_DEFINITION = """
    scan_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN (
            'QUEUED','DATA','METRICS','LLM','REPORT','DONE','DEGRADED','FAILED'
        )
    ),
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    error_message TEXT,
    failed_stage TEXT,
    report_id TEXT,
    data_quality TEXT,
    llm_status TEXT,
    llm_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd TEXT NOT NULL DEFAULT '0',
    cost_unpriced INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scan_id, symbol)
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
    starred INTEGER NOT NULL DEFAULT 0,
    starred_at TEXT,
    CHECK (tradifi IN (0, 1)),
    CHECK (starred IN (0, 1)),
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
        created_at TEXT NOT NULL,
        report_id TEXT,
        source_mode TEXT NOT NULL DEFAULT 'FIXTURE',
        metrics_manifest_json TEXT,
        error_code TEXT,
        error_message TEXT,
        batch_id TEXT,
        llm_configured INTEGER NOT NULL DEFAULT 0,
        llm_status TEXT,
        llm_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd TEXT NOT NULL DEFAULT '0',
        cost_unpriced INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS scans_created_at ON scans(created_at);
    CREATE INDEX IF NOT EXISTS scans_batch_id ON scans(batch_id);
    CREATE TABLE IF NOT EXISTS reports (
        report_id TEXT PRIMARY KEY,
        scan_id TEXT NOT NULL REFERENCES scans(scan_id),
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        metrics_manifest_json TEXT NOT NULL,
        llm_decision_id TEXT,
        narrative_ref TEXT,
        data_quality TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        llm_json TEXT,
        window_policy_json TEXT
    );
    CREATE INDEX IF NOT EXISTS reports_generated_at ON reports(generated_at);
    CREATE INDEX IF NOT EXISTS reports_scan_id ON reports(scan_id);
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        response_status INTEGER NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (scope, key)
    );
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
        status TEXT NOT NULL,
        metric_version TEXT,
        input_manifest_hash TEXT,
        window_start TEXT,
        window_end TEXT,
        run_id TEXT,
        last_n INTEGER,
        recomputed_from TEXT,
        parent_run_id TEXT
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
    CREATE TABLE IF NOT EXISTS job_runs ({_JOB_RUNS_DEFINITION});
    CREATE INDEX IF NOT EXISTS job_runs_type_status ON job_runs(job_type, status);
    CREATE INDEX IF NOT EXISTS job_runs_type_heartbeat ON job_runs(job_type, heartbeat_at);
    CREATE INDEX IF NOT EXISTS job_runs_window ON job_runs(job_type, window_start);
    CREATE TABLE IF NOT EXISTS job_locks ({_JOB_LOCKS_DEFINITION});
    CREATE TABLE IF NOT EXISTS scan_symbols ({_SCAN_SYMBOLS_DEFINITION});
    CREATE INDEX IF NOT EXISTS scan_symbols_stage ON scan_symbols(scan_id, stage);
    CREATE INDEX IF NOT EXISTS manifest_symbol_date ON manifest(symbol, date);
    CREATE TABLE IF NOT EXISTS llm_calls (
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
    CREATE INDEX IF NOT EXISTS llm_calls_decision ON llm_calls(decision_id);
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        validation_status TEXT NOT NULL,
        raw_output_hash TEXT NOT NULL,
        persisted_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS decisions_status ON decisions(validation_status);
    CREATE TABLE IF NOT EXISTS budget_ledger (
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
    CREATE INDEX IF NOT EXISTS budget_ledger_day ON budget_ledger(period_day);
    CREATE INDEX IF NOT EXISTS budget_ledger_week ON budget_ledger(period_week);
    CREATE INDEX IF NOT EXISTS budget_ledger_month ON budget_ledger(period_month);
    CREATE TABLE IF NOT EXISTS budget_settings (
        id TEXT PRIMARY KEY CHECK (id = 'default'),
        daily_usd TEXT NOT NULL,
        weekly_usd TEXT NOT NULL,
        monthly_usd TEXT NOT NULL,
        warn_pct REAL NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_capabilities (
        provider TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        strict_schema INTEGER NOT NULL,
        degraded INTEGER NOT NULL,
        max_output_tokens INTEGER NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        notes TEXT NOT NULL,
        latency_ms INTEGER NOT NULL,
        checked_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS opportunities (
        opportunity_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('OPEN','TRIGGERED','EXPIRED','INVALIDATED')
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS opportunities_state ON opportunities(state);
    CREATE TABLE IF NOT EXISTS opportunity_events (
        opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
        seq INTEGER NOT NULL,
        at TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        note TEXT NOT NULL,
        PRIMARY KEY (opportunity_id, seq)
    );
    CREATE TABLE IF NOT EXISTS trigger_events (
        trigger_id TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
        symbol TEXT NOT NULL,
        status TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        triggered_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS trigger_events_opportunity
        ON trigger_events(opportunity_id);
    CREATE TABLE IF NOT EXISTS risk_verdicts (
        risk_verdict_id TEXT PRIMARY KEY,
        trigger_id TEXT NOT NULL REFERENCES trigger_events(trigger_id),
        decision_id TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('ALLOW','REDUCE','BLOCK')),
        binding_cap TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        client_order_id TEXT NOT NULL UNIQUE,
        environment TEXT NOT NULL,
        symbol TEXT NOT NULL,
        status TEXT NOT NULL,
        risk_verdict_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(order_id),
        environment TEXT NOT NULL,
        symbol TEXT NOT NULL,
        filled_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS positions (
        position_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        symbol TEXT NOT NULL,
        state TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS positions_state ON positions(state);
    -- Slice 11 replay/pagination indexes. ``fills(order_id)`` is mandatory:
    -- it is an FK and the accept replay looks the fill up by order.
    CREATE INDEX IF NOT EXISTS fills_order_id ON fills(order_id);
    CREATE INDEX IF NOT EXISTS budget_ledger_decision ON budget_ledger(decision_id);
    CREATE INDEX IF NOT EXISTS llm_calls_role ON llm_calls(role);
    CREATE INDEX IF NOT EXISTS decisions_role ON decisions(role);
    CREATE INDEX IF NOT EXISTS trigger_events_triggered_at ON trigger_events(triggered_at);
    CREATE INDEX IF NOT EXISTS trigger_events_status_triggered
        ON trigger_events(status, triggered_at);
    CREATE INDEX IF NOT EXISTS opportunities_state_created
        ON opportunities(state, created_at);
    CREATE INDEX IF NOT EXISTS opportunities_state_symbol ON opportunities(state, symbol);
    CREATE INDEX IF NOT EXISTS orders_environment_status_symbol
        ON orders(environment, status, symbol);
    CREATE INDEX IF NOT EXISTS positions_environment_state ON positions(environment, state);
    CREATE TABLE IF NOT EXISTS paper_accounts (
        account_id TEXT PRIMARY KEY,
        environment TEXT NOT NULL,
        currency TEXT NOT NULL,
        initial_balance TEXT NOT NULL,
        balance TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS prompt_templates (
        prompt_id TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        active_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS prompt_versions (
        prompt_id TEXT NOT NULL REFERENCES prompt_templates(prompt_id),
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
        created_at TEXT NOT NULL,
        system_prompt_id TEXT NOT NULL DEFAULT '',
        system_prompt_version INTEGER NOT NULL DEFAULT 1,
        system_body TEXT NOT NULL DEFAULT '',
        system_body_hash TEXT NOT NULL DEFAULT '',
        required_variables_json TEXT NOT NULL DEFAULT '[]',
        knowledge_artifact_hash TEXT,
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK (status IN ('DRAFT', 'ACTIVE', 'SUPERSEDED')),
        author TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (prompt_id, version),
        CHECK ((status = 'ACTIVE') = (is_active = 1))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_one_active
        ON prompt_versions(prompt_id) WHERE is_active = 1;
    DROP TRIGGER IF EXISTS prompt_versions_immutable;
    CREATE TRIGGER prompt_versions_immutable
        BEFORE UPDATE OF prompt_id, version, body, body_hash, created_at,
            system_prompt_id, system_prompt_version, system_body,
            system_body_hash, required_variables_json, knowledge_artifact_hash
        ON prompt_versions
    BEGIN
        SELECT RAISE(ABORT, 'prompt_versions rows are immutable');
    END;
    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
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


def _migrate_symbols_v7(connection: sqlite3.Connection) -> None:
    """Add the starred/starred_at columns to an existing symbols table."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(symbols)")}
    missing = [name for name in ("starred", "starred_at") if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        if "starred" in missing:
            connection.execute("ALTER TABLE symbols ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        if "starred_at" in missing:
            connection.execute("ALTER TABLE symbols ADD COLUMN starred_at TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_manifest_v6(connection: sqlite3.Connection) -> None:
    """Add nullable metrics provenance columns to the manifest table."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(manifest)")}
    needed = ("metric_version", "input_manifest_hash", "window_start", "window_end")
    missing = [name for name in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name in missing:
            connection.execute(f"ALTER TABLE manifest ADD COLUMN {name} TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_manifest_v8(connection: sqlite3.Connection) -> None:
    """Add explicit bar-run provenance columns to the manifest table."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(manifest)")}
    needed = (
        ("run_id", "TEXT"),
        ("last_n", "INTEGER"),
        ("recomputed_from", "TEXT"),
        ("parent_run_id", "TEXT"),
    )
    missing = [(name, kind) for name, kind in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE manifest ADD COLUMN {name} {kind}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_reports_v9(connection: sqlite3.Connection) -> None:
    """Add report linkage columns to an existing scans table (v9)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    needed = (
        ("report_id", "TEXT"),
        ("source_mode", "TEXT NOT NULL DEFAULT 'FIXTURE'"),
        ("metrics_manifest_json", "TEXT"),
    )
    missing = [(name, kind) for name, kind in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE scans ADD COLUMN {name} {kind}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_scans_v9(connection: sqlite3.Connection) -> None:
    """Add explicit failure diagnostics to the scans table."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    needed = (("error_code", "TEXT"), ("error_message", "TEXT"))
    missing = [(name, kind) for name, kind in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE scans ADD COLUMN {name} {kind}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_reports_v10(connection: sqlite3.Connection) -> None:
    """Add the additive LLM band column to an existing reports table (v10)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
    if "llm_json" in columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE reports ADD COLUMN llm_json TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_reports_v11(connection: sqlite3.Connection) -> None:
    """Add the additive window-policy column to an existing reports table (v11)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
    if "window_policy_json" in columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE reports ADD COLUMN window_policy_json TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_scans_v11(connection: sqlite3.Connection) -> None:
    """Add batch identity and LLM cost fields to an existing scans table (v11)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scans)")}
    needed = (
        ("batch_id", "TEXT"),
        ("llm_configured", "INTEGER NOT NULL DEFAULT 0"),
        ("llm_status", "TEXT"),
        ("llm_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd", "TEXT NOT NULL DEFAULT '0'"),
        ("cost_unpriced", "INTEGER NOT NULL DEFAULT 1"),
    )
    missing = [(name, kind) for name, kind in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE scans ADD COLUMN {name} {kind}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_scan_symbols_v11(connection: sqlite3.Connection) -> None:
    """Rebuild ``scan_symbols`` for the LLM/DEGRADED stages and cost fields (v11).

    SQLite cannot widen a CHECK constraint with ``ALTER TABLE``, so the table is
    recreated and copied. The migration is idempotent: a table that already has
    the new definition is left untouched.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'scan_symbols'"
    ).fetchone()
    if row is None or row[0] is None:
        return
    definition = str(row[0])
    columns = {item[1] for item in connection.execute("PRAGMA table_info(scan_symbols)")}
    needs_rebuild = (
        "'LLM'" not in definition
        or "'DEGRADED'" not in definition
        or "error_message" not in columns
        or "llm_tokens" not in columns
        or "cost_usd" not in columns
        or "cost_unpriced" not in columns
    )
    if not needs_rebuild:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE scan_symbols RENAME TO scan_symbols_v10")
        connection.execute(f"CREATE TABLE scan_symbols ({_SCAN_SYMBOLS_DEFINITION})")
        copied = [
            name
            for name in (
                "scan_id",
                "symbol",
                "stage",
                "started_at",
                "finished_at",
                "error_code",
                "report_id",
                "data_quality",
            )
            if name in columns
        ]
        if copied:
            column_list = ", ".join(copied)
            connection.execute(
                f"INSERT INTO scan_symbols ({column_list}) "
                f"SELECT {column_list} FROM scan_symbols_v10"
            )
        connection.execute("DROP TABLE scan_symbols_v10")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_scan_symbols_v15(connection: sqlite3.Connection) -> None:
    """Add the failing stage so a FAILED row renders the real stage (v15)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_symbols)")}
    if "failed_stage" in columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE scan_symbols ADD COLUMN failed_stage TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_job_runs_v12(connection: sqlite3.Connection) -> None:
    """Persist the manual scan options needed to resume a reclaimed batch (v12)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(job_runs)")}
    needed = (
        ("source", "TEXT NOT NULL DEFAULT 'backfill'"),
        ("metrics_only", "INTEGER NOT NULL DEFAULT 0"),
        ("provider", "TEXT"),
    )
    missing = [(name, kind) for name, kind in needed if name not in columns]
    if not missing:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, kind in missing:
            connection.execute(f"ALTER TABLE job_runs ADD COLUMN {name} {kind}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_job_runs_v13(connection: sqlite3.Connection) -> None:
    """Persist the target symbol so a single-symbol run resumes its own scope (v13)."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(job_runs)")}
    if "symbol" in columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE job_runs ADD COLUMN symbol TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_job_runs_v14(connection: sqlite3.Connection) -> None:
    """Add the persisted scan period and the ``PARTIAL`` batch status (v14).

    SQLite cannot widen a CHECK constraint with ``ALTER TABLE``, so the table is
    recreated and copied when the old definition is detected. The migration is
    idempotent: a table that already has the period column and the wider status
    check is left untouched.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_runs'"
    ).fetchone()
    if row is None or row[0] is None:
        return
    definition = str(row[0])
    columns = {item[1] for item in connection.execute("PRAGMA table_info(job_runs)")}
    needs_rebuild = "'PARTIAL'" not in definition
    needs_period = "period" not in columns
    if not needs_rebuild and not needs_period:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        if needs_rebuild:
            connection.execute("ALTER TABLE job_runs RENAME TO job_runs_v13")
            connection.execute(f"CREATE TABLE job_runs ({_JOB_RUNS_DEFINITION})")
            copied = [
                name
                for name in (
                    "run_id",
                    "job_type",
                    "window_start",
                    "status",
                    "trigger_source",
                    "started_at",
                    "finished_at",
                    "error_code",
                    "message",
                    "lease_owner",
                    "lease_expires_at",
                    "heartbeat_at",
                    "source",
                    "metrics_only",
                    "provider",
                    "symbol",
                    "period",
                )
                if name in columns
            ]
            if copied:
                column_list = ", ".join(copied)
                connection.execute(
                    f"INSERT INTO job_runs ({column_list}) SELECT {column_list} FROM job_runs_v13"
                )
            connection.execute("DROP TABLE job_runs_v13")
        else:
            connection.execute("ALTER TABLE job_runs ADD COLUMN period TEXT")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


_PROMPT_SEED_CREATED_AT = "2026-11-08T18:00:00.000Z"
_PROMPT_VERSION_ADDITIONS = (
    ("system_prompt_id", "TEXT NOT NULL DEFAULT ''"),
    ("system_prompt_version", "INTEGER NOT NULL DEFAULT 1"),
    ("system_body", "TEXT NOT NULL DEFAULT ''"),
    ("system_body_hash", "TEXT NOT NULL DEFAULT ''"),
    ("required_variables_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("knowledge_artifact_hash", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ("author", "TEXT NOT NULL DEFAULT ''"),
    ("reason", "TEXT NOT NULL DEFAULT ''"),
    ("is_default", "INTEGER NOT NULL DEFAULT 0"),
)
_LLM_CALL_ADDITIONS = (
    ("system_prompt_id", "TEXT NOT NULL DEFAULT ''"),
    ("system_prompt_version", "INTEGER NOT NULL DEFAULT 1"),
    ("system_prompt_hash", "TEXT NOT NULL DEFAULT ''"),
)
_BUDGET_LEDGER_ADDITIONS = (
    ("period_week", "TEXT NOT NULL DEFAULT ''"),
    ("period_month", "TEXT NOT NULL DEFAULT ''"),
    ("role", "TEXT NOT NULL DEFAULT ''"),
)


def _period_parts(occurred_at: str) -> tuple[str, str, str]:
    """UTC day/week/month buckets; the canonical helper is shared (M4)."""
    return period_parts(occurred_at)


def _add_missing_columns(
    connection: sqlite3.Connection, table: str, additions: tuple[tuple[str, str], ...]
) -> None:
    if not _table_exists(connection, table):
        return
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in additions:
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_prompts_v16(connection: sqlite3.Connection) -> None:
    """Add the ADR-0009 prompt-version and system-prompt columns (v16)."""
    prompt_columns_added = _table_exists(connection, "prompt_versions") and any(
        name not in {row[1] for row in connection.execute("PRAGMA table_info(prompt_versions)")}
        for name, _definition in _PROMPT_VERSION_ADDITIONS
    )
    llm_columns_added = _table_exists(connection, "llm_calls") and any(
        name not in {row[1] for row in connection.execute("PRAGMA table_info(llm_calls)")}
        for name, _definition in _LLM_CALL_ADDITIONS
    )
    if not prompt_columns_added and not llm_columns_added:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(connection, "prompt_versions", _PROMPT_VERSION_ADDITIONS)
        _add_missing_columns(connection, "llm_calls", _LLM_CALL_ADDITIONS)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_v17(connection: sqlite3.Connection) -> None:
    """Add budget period/role columns, budget settings, and provider probes (v17).

    The backfill runs whenever the recorded version is below 17 (the caller
    already guards on that), even if the columns already exist, so a partially
    migrated v16 database still gains its week/month buckets (M5). A malformed
    legacy ``occurred_at`` keeps its defaults and is logged instead of aborting
    the transaction and the daemon boot (M6).
    """
    if not _table_exists(connection, "budget_ledger"):
        return
    existing = {row[1] for row in connection.execute("PRAGMA table_info(budget_ledger)")}
    missing = [name for name, _definition in _BUDGET_LEDGER_ADDITIONS if name not in existing]
    has_pending = False
    if not missing:
        pending_backfill = connection.execute(
            "SELECT COUNT(*) FROM budget_ledger "
            "WHERE period_week = '' OR period_month = '' OR period_day = ''"
        ).fetchone()
        has_pending = bool(pending_backfill and int(pending_backfill[0]) > 0)
    if not missing and not has_pending:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(connection, "budget_ledger", _BUDGET_LEDGER_ADDITIONS)
        rows = connection.execute(
            "SELECT record_id, occurred_at FROM budget_ledger "
            "WHERE period_week = '' OR period_month = '' OR period_day = ''"
        ).fetchall()
        for row in rows:
            record_id = str(row[0])
            try:
                day, week, month = _period_parts(str(row[1]))
            except (TypeError, ValueError, OverflowError):
                _LOGGER.warning(
                    "budget_period_backfill_skipped",
                    extra={"record_id": record_id, "occurred_at": str(row[1])},
                )
                continue
            connection.execute(
                """
                UPDATE budget_ledger
                SET period_day = CASE WHEN period_day = '' THEN ? ELSE period_day END,
                    period_week = CASE WHEN period_week = '' THEN ? ELSE period_week END,
                    period_month = CASE WHEN period_month = '' THEN ? ELSE period_month END
                WHERE record_id = ?
                """,
                (day, week, month, record_id),
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _seed_prompt_defaults(connection: sqlite3.Connection) -> None:
    """Seed the built-in ANALYSIS/ENTRY_RECHECK templates as active v1.

    ``INSERT OR IGNORE`` keeps the seed idempotent; an operator's edits and
    activations are never overwritten by a later initialize.
    """
    now = _PROMPT_SEED_CREATED_AT
    for role in (PromptRole.ANALYSIS, PromptRole.ENTRY_RECHECK):
        user_id = PROMPT_IDS[role]
        system_id = SYSTEM_PROMPT_IDS[role]
        for prompt_id in (user_id, system_id):
            connection.execute(
                """
                INSERT OR IGNORE INTO prompt_templates
                    (prompt_id, role, active_version, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (prompt_id, role.value, now, now),
            )
        system_body = DEFAULT_SYSTEM_BODIES[role]
        system_hash = content_hash(system_body)
        connection.execute(
            """
            INSERT OR IGNORE INTO prompt_versions (
                prompt_id, version, body, body_hash, is_active, created_at,
                system_prompt_id, system_prompt_version, system_body,
                system_body_hash, required_variables_json, knowledge_artifact_hash,
                status, author, reason, is_default
            ) VALUES (?, 1, ?, ?, 1, ?, ?, 1, ?, ?, '[]', NULL, ?, 'system', 'built-in default', 1)
            """,
            (
                system_id,
                system_body,
                system_hash,
                now,
                system_id,
                system_body,
                system_hash,
                PromptStatus.ACTIVE.value,
            ),
        )
        user_body = DEFAULT_USER_BODIES[role]
        connection.execute(
            """
            INSERT OR IGNORE INTO prompt_versions (
                prompt_id, version, body, body_hash, is_active, created_at,
                system_prompt_id, system_prompt_version, system_body,
                system_body_hash, required_variables_json, knowledge_artifact_hash,
                status, author, reason, is_default
            ) VALUES (?, 1, ?, ?, 1, ?, ?, 1, ?, ?, ?, NULL, ?, 'system', 'built-in default', 1)
            """,
            (
                user_id,
                user_body,
                content_hash(user_body),
                now,
                system_id,
                system_body,
                system_hash,
                json.dumps(list(REQUIRED_VARIABLES[role])),
                PromptStatus.ACTIVE.value,
            ),
        )


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
        if _schema_version(connection) < 6 and _table_exists(connection, "manifest"):
            _migrate_manifest_v6(connection)
        if _schema_version(connection) < 7 and _table_exists(connection, "symbols"):
            _migrate_symbols_v7(connection)
        if _schema_version(connection) < 8 and _table_exists(connection, "manifest"):
            _migrate_manifest_v8(connection)
        if _schema_version(connection) < 9 and _table_exists(connection, "scans"):
            _migrate_scans_v9(connection)
        # Report linkage is additive; keep the recorded schema version stable so
        # older migration expectations remain valid while the columns land.
        if _table_exists(connection, "scans"):
            _migrate_reports_v9(connection)
        if _table_exists(connection, "reports"):
            _migrate_reports_v10(connection)
            _migrate_reports_v11(connection)
        if _table_exists(connection, "scans"):
            _migrate_scans_v11(connection)
        if _table_exists(connection, "scan_symbols"):
            _migrate_scan_symbols_v11(connection)
            _migrate_scan_symbols_v15(connection)
        if _table_exists(connection, "job_runs"):
            _migrate_job_runs_v12(connection)
            _migrate_job_runs_v13(connection)
            _migrate_job_runs_v14(connection)
        if _schema_version(connection) < 16:
            _migrate_prompts_v16(connection)
        if _schema_version(connection) < 17:
            _migrate_v17(connection)
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
                (5, "2026-11-06T17:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (6, "2026-11-06T18:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (7, "2026-11-06T18:30:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (8, "2026-11-06T19:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (9, "2026-11-06T20:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (10, "2026-11-08T12:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (11, "2026-11-08T13:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (12, "2026-11-08T14:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (13, "2026-11-08T15:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (14, "2026-11-08T16:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (15, "2026-11-08T17:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (16, "2026-11-08T18:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, "2026-11-09T00:00:00.000Z"),
            )
            _seed_prompt_defaults(connection)
    finally:
        connection.close()
        # The DB may hold a trading journal; never leave it group/world readable.
        os.chmod(db_path, 0o600)


def _row_to_scan(row: sqlite3.Row) -> ScanRecord:
    keys = row.keys()
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
        report_id=row["report_id"] if "report_id" in keys else None,
        source_mode=row["source_mode"] if "source_mode" in keys else "FIXTURE",
        metrics_manifest_json=(
            row["metrics_manifest_json"] if "metrics_manifest_json" in keys else None
        ),
        error_code=row["error_code"] if "error_code" in keys else None,
        error_message=row["error_message"] if "error_message" in keys else None,
        batch_id=row["batch_id"] if "batch_id" in keys else None,
        llm_configured=(bool(row["llm_configured"]) if "llm_configured" in keys else False),
        llm_status=row["llm_status"] if "llm_status" in keys else None,
        llm_tokens=int(row["llm_tokens"]) if "llm_tokens" in keys else 0,
        cost_usd=row["cost_usd"] if "cost_usd" in keys else "0",
        cost_unpriced=bool(row["cost_unpriced"]) if "cost_unpriced" in keys else True,
    )


def _row_to_report(row: sqlite3.Row) -> ReportRecord:
    keys = row.keys()
    return ReportRecord(
        report_id=row["report_id"],
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        generated_at=row["generated_at"],
        metrics_manifest_json=row["metrics_manifest_json"],
        llm_decision_id=row["llm_decision_id"],
        narrative_ref=row["narrative_ref"],
        data_quality=row["data_quality"],
        snapshot_json=row["snapshot_json"],
        metrics_json=row["metrics_json"],
        llm_json=row["llm_json"] if "llm_json" in keys else None,
        window_policy_json=(row["window_policy_json"] if "window_policy_json" in keys else None),
    )


def _row_to_idempotency(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        scope=row["scope"],
        key=row["key"],
        request_hash=row["request_hash"],
        response_status=row["response_status"],
        response_json=row["response_json"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
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
        metric_version=row["metric_version"],
        input_manifest_hash=row["input_manifest_hash"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        run_id=row["run_id"],
        last_n=row["last_n"],
        recomputed_from=row["recomputed_from"],
        parent_run_id=row["parent_run_id"],
        run_seq=row["run_seq"],
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


def _row_to_job_run(row: sqlite3.Row) -> JobRunRecord:
    keys = row.keys()
    return JobRunRecord(
        run_id=row["run_id"],
        job_type=row["job_type"],
        window_start=row["window_start"],
        status=row["status"],
        trigger_source=row["trigger_source"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        message=row["message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        source=row["source"] if "source" in keys else "backfill",
        metrics_only=(bool(row["metrics_only"]) if "metrics_only" in keys else False),
        provider=row["provider"] if "provider" in keys else None,
        symbol=row["symbol"] if "symbol" in keys else None,
        period=row["period"] if "period" in keys else None,
    )


def _row_to_job_lock(row: sqlite3.Row) -> JobLockRecord:
    return JobLockRecord(
        lock_name=row["lock_name"],
        owner=row["owner"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )


def _row_to_scan_symbol(row: sqlite3.Row) -> ScanSymbolRecord:
    keys = row.keys()
    return ScanSymbolRecord(
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        stage=row["stage"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        report_id=row["report_id"],
        data_quality=row["data_quality"],
        error_message=row["error_message"] if "error_message" in keys else None,
        failed_stage=row["failed_stage"] if "failed_stage" in keys else None,
        llm_status=row["llm_status"] if "llm_status" in keys else None,
        llm_tokens=int(row["llm_tokens"]) if "llm_tokens" in keys else 0,
        cost_usd=row["cost_usd"] if "cost_usd" in keys else "0",
        cost_unpriced=bool(row["cost_unpriced"]) if "cost_unpriced" in keys else True,
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
                manifest_path, parquet_rows, created_at, report_id, source_mode,
                metrics_manifest_json, error_code, error_message, batch_id,
                llm_configured, llm_status, llm_tokens, cost_usd, cost_unpriced
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
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
                record.report_id,
                record.source_mode,
                record.metrics_manifest_json,
                record.error_code,
                record.error_message,
                record.batch_id,
                int(record.llm_configured),
                record.llm_status,
                record.llm_tokens,
                record.cost_usd,
                int(record.cost_unpriced),
            ),
        )

    def update_scan(self, record: ScanRecord) -> None:
        """Update the mutable lifecycle fields of an existing scan row."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE scans SET
                    status = ?, finished_at = ?, data_quality = ?, report_id = ?,
                    trade_count = ?, buy_volume = ?, sell_volume = ?, delta = ?,
                    cvd = ?, manifest_path = ?, parquet_rows = ?,
                    metrics_manifest_json = ?, error_code = ?, error_message = ?,
                    batch_id = ?, llm_configured = ?, llm_status = ?,
                    llm_tokens = ?, cost_usd = ?, cost_unpriced = ?
                WHERE scan_id = ?
                """,
                (
                    record.status,
                    record.finished_at,
                    record.data_quality,
                    record.report_id,
                    record.trade_count,
                    record.buy_volume,
                    record.sell_volume,
                    record.delta,
                    record.cvd,
                    record.manifest_path,
                    record.parquet_rows,
                    record.metrics_manifest_json,
                    record.error_code,
                    record.error_message,
                    record.batch_id,
                    int(record.llm_configured),
                    record.llm_status,
                    record.llm_tokens,
                    record.cost_usd,
                    int(record.cost_unpriced),
                    record.scan_id,
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

    def list_scans(self, limit: int = 20, offset: int = 0) -> tuple[ScanRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scans ORDER BY created_at DESC, scan_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan(row) for row in rows)

    def list_scans_by_batch(self, batch_id: str) -> tuple[ScanRecord, ...]:
        """Return every child scan of one batch, ordered by symbol.

        Uses the ``scans(batch_id)`` index so a batch whose rows are older than
        the newest page of scans still renders its full window and quality.
        """
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scans WHERE batch_id = ? ORDER BY symbol", (batch_id,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan(row) for row in rows)

    def count_scans(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) FROM scans").fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    # -- Reports ---------------------------------------------------------

    def save_report(self, record: ReportRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_report(connection, record)

    def _insert_report(self, connection: sqlite3.Connection, record: ReportRecord) -> None:
        connection.execute(
            """
            INSERT INTO reports (
                report_id, scan_id, symbol, timeframe, generated_at,
                metrics_manifest_json, llm_decision_id, narrative_ref,
                data_quality, snapshot_json, metrics_json, llm_json,
                window_policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.report_id,
                record.scan_id,
                record.symbol,
                record.timeframe,
                record.generated_at,
                record.metrics_manifest_json,
                record.llm_decision_id,
                record.narrative_ref,
                record.data_quality,
                record.snapshot_json,
                record.metrics_json,
                record.llm_json,
                record.window_policy_json,
            ),
        )

    def save_report_and_complete_scan(self, report: ReportRecord, scan: ScanRecord) -> None:
        """Insert the report and finish the scan in one transaction.

        A failure in either statement rolls both back, so a report can never be
        committed without the matching DONE scan row.
        """
        with closing(self._connect()) as connection, connection:
            self._insert_report(connection, report)
            cursor = connection.execute(
                """
                UPDATE scans SET
                    status = ?, finished_at = ?, data_quality = ?, report_id = ?,
                    trade_count = ?, buy_volume = ?, sell_volume = ?, delta = ?,
                    cvd = ?, manifest_path = ?, parquet_rows = ?,
                    metrics_manifest_json = ?, error_code = ?, error_message = ?,
                    batch_id = ?, llm_configured = ?, llm_status = ?,
                    llm_tokens = ?, cost_usd = ?, cost_unpriced = ?
                WHERE scan_id = ?
                """,
                (
                    scan.status,
                    scan.finished_at,
                    scan.data_quality,
                    scan.report_id,
                    scan.trade_count,
                    scan.buy_volume,
                    scan.sell_volume,
                    scan.delta,
                    scan.cvd,
                    scan.manifest_path,
                    scan.parquet_rows,
                    scan.metrics_manifest_json,
                    scan.error_code,
                    scan.error_message,
                    scan.batch_id,
                    int(scan.llm_configured),
                    scan.llm_status,
                    scan.llm_tokens,
                    scan.cost_usd,
                    int(scan.cost_unpriced),
                    scan.scan_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreUnavailableError(
                    f"scan {scan.scan_id} disappeared before report completion"
                )

    def fail_stale_scans(self, *, now: str, older_than_seconds: int = 900) -> int:
        """Mark RUNNING scans older than the lease window as FAILED."""
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        stale_before = utc_timestamp(now_dt - timedelta(seconds=older_than_seconds))
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE scans SET
                    status = 'FAILED', finished_at = ?, data_quality = 'MISSING',
                    error_code = 'SCAN_STALE',
                    error_message = 'scan exceeded the RUNNING lease and was failed'
                WHERE status = 'RUNNING' AND started_at < ?
                """,
                (now, stale_before),
            )
        return cursor.rowcount

    def get_report(self, report_id: str) -> ReportRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_report(row) if row is not None else None

    def list_reports(
        self, *, scan_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[ReportRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where = " WHERE scan_id = ?" if scan_id is not None else ""
        params: tuple[object, ...] = (
            (scan_id, limit, offset) if scan_id is not None else (limit, offset)
        )
        try:
            rows = connection.execute(
                f"SELECT * FROM reports{where} "
                "ORDER BY generated_at DESC, report_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_report(row) for row in rows)

    def count_reports(self, *, scan_id: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where = " WHERE scan_id = ?" if scan_id is not None else ""
        params: tuple[object, ...] = (scan_id,) if scan_id is not None else ()
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM reports{where}", params).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    # -- Idempotency -----------------------------------------------------

    def get_idempotency(self, *, scope: str, key: str) -> IdempotencyRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_idempotency(row) if row is not None else None

    def reserve_idempotency(self, record: IdempotencyRecord) -> bool:
        """Atomically reserve a key; False means the key already exists."""
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, key, request_hash, response_status, response_json,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                        request_hash = excluded.request_hash,
                        response_status = excluded.response_status,
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    WHERE idempotency_keys.expires_at <= excluded.created_at
                    """,
                    (
                        record.scope,
                        record.key,
                        record.request_hash,
                        record.response_status,
                        record.response_json,
                        record.created_at,
                        record.expires_at,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return cursor.rowcount == 1

    def renew_idempotency(self, *, scope: str, key: str, expires_at: str) -> None:
        """Extend an in-flight reservation lease without touching its payload."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE idempotency_keys SET expires_at = ? "
                "WHERE scope = ? AND key = ? AND response_json = ''",
                (expires_at, scope, key),
            )

    def complete_idempotency(self, record: IdempotencyRecord) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE idempotency_keys SET
                    request_hash = ?, response_status = ?, response_json = ?,
                    created_at = ?, expires_at = ?
                WHERE scope = ? AND key = ?
                """,
                (
                    record.request_hash,
                    record.response_status,
                    record.response_json,
                    record.created_at,
                    record.expires_at,
                    record.scope,
                    record.key,
                ),
            )
            if cursor.rowcount != 1:
                # The lease expired or was purged while the request ran. Re-insert
                # the completed record so the response is not silently lost; the
                # replay path must still be able to return it.
                _LOGGER.warning(
                    "idempotency lease for %s/%s vanished before completion; re-inserting",
                    record.scope,
                    record.key,
                )
                connection.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, key, request_hash, response_status, response_json,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                        request_hash = excluded.request_hash,
                        response_status = excluded.response_status,
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        record.scope,
                        record.key,
                        record.request_hash,
                        record.response_status,
                        record.response_json,
                        record.created_at,
                        record.expires_at,
                    ),
                )

    def delete_idempotency(self, *, scope: str, key: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key)
            )

    def purge_expired_idempotency(self, *, now: str) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= ?", (now,)
            )
        return cursor.rowcount

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys (
                    scope, key, request_hash, response_status, response_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.scope,
                    record.key,
                    record.request_hash,
                    record.response_status,
                    record.response_json,
                    record.created_at,
                    record.expires_at,
                ),
            )

    def record_manifest(self, entry: ManifestEntry) -> None:
        # Scans/backfills are append-only: a duplicate path is a real conflict.
        with closing(self._connect()) as connection, connection:
            self._insert_manifest(connection, entry)

    def upsert_manifest_entry(self, entry: ManifestEntry) -> None:
        """Metrics-only idempotent manifest write (re-persist same content)."""
        try:
            with closing(self._connect()) as connection, connection:
                self._insert_manifest(connection, entry, upsert=True)
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest write failed: {exc}") from exc

    def upsert_manifest_entries(self, entries: tuple[ManifestEntry, ...]) -> None:
        """Publish metrics + footprint manifest rows in one transaction."""
        try:
            with closing(self._connect()) as connection, connection:
                for entry in entries:
                    self._insert_manifest(connection, entry, upsert=True)
        except sqlite3.Error as exc:
            # "database is locked"/disk-full are OperationalError, not OSError;
            # map them so the supervisor treats them as fatal.
            raise StoreUnavailableError(f"manifest write failed: {exc}") from exc

    def _insert_manifest(
        self, connection: sqlite3.Connection, entry: ManifestEntry, *, upsert: bool = False
    ) -> None:
        conflict = "ON CONFLICT(path) DO NOTHING" if upsert else ""
        connection.execute(
            f"""
            INSERT INTO manifest (
                path, data_type, symbol, date, rows, bytes, checksum,
                source, created_at, status, metric_version, input_manifest_hash,
                window_start, window_end, run_id, last_n, recomputed_from,
                parent_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            {conflict}
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
                entry.metric_version,
                entry.input_manifest_hash,
                entry.window_start,
                entry.window_end,
                entry.run_id,
                entry.last_n,
                entry.recomputed_from,
                entry.parent_run_id,
            ),
        )

    def delete_manifest_entry(self, path: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM manifest WHERE path = ?", (path,))

    def get_manifest_entry(self, path: str) -> ManifestEntry | None:
        """Return one manifest row; raises when the DB itself is unreadable."""
        connection = self._connect_read_only()
        if connection is None:
            raise StoreUnavailableError("manifest database is unreadable")
        try:
            row = connection.execute(
                "SELECT rowid AS run_seq, * FROM manifest WHERE path = ?", (path,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest lookup failed: {exc}") from exc
        finally:
            connection.close()
        return _row_to_manifest(row) if row is not None else None

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
        try:
            connection = self._connect_read_only()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest database is unreadable: {exc}") from exc
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT rowid AS run_seq, * FROM manifest{where} ORDER BY created_at, path",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest read failed: {exc}") from exc
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
        starred: bool | None = None,
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
        if starred is not None:
            clauses.append("starred = ?")
            params.append(int(starred))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        starred: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type, starred=starred
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
        starred: bool | None = None,
    ) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type, starred=starred
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

    def set_starred(self, symbol: str, starred: bool, *, starred_at: str | None = None) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE symbols SET starred = ?, starred_at = ? WHERE symbol = ?",
                (int(starred), starred_at if starred else None, symbol),
            )
            return cursor.rowcount > 0

    def list_starred(self) -> tuple[SymbolContract, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM symbols WHERE starred = 1 ORDER BY symbol"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"starred symbol read failed: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_symbol(row) for row in rows)

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
        owner: str | None = None,
        replace_date: bool = False,
    ) -> None:
        """Unit of work: part manifest + gap resolve + COMPLETE row, COMPLETE last.

        The COMPLETE row must never survive without its published parts; a
        failure rolls the whole transaction back so the caller can delete the
        parts and retry safely. When ``owner`` is supplied the run must still be
        ``RUNNING`` under that owner, otherwise ``LeaseLostError`` aborts.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if owner is not None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (resolved_at, row.run_id, owner),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise LeaseLostError()
            for entry in entries:
                self._insert_part_manifest(connection, entry)
            if replace_date:
                paths = [entry.path for entry in entries]
                if paths:
                    placeholders = ", ".join("?" for _ in paths)
                    connection.execute(
                        f"DELETE FROM manifest WHERE symbol = ? AND date = ? "
                        f"AND (source LIKE 'archive:%' OR source = 'recovered:orphan') "
                        f"AND path NOT IN ({placeholders})",
                        (row.symbol, row.date, *paths),
                    )
                else:
                    connection.execute(
                        "DELETE FROM manifest WHERE symbol = ? AND date = ? "
                        "AND (source LIKE 'archive:%' OR source = 'recovered:orphan')",
                        (row.symbol, row.date),
                    )
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
        owner: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Downgrade a published COMPLETE row and drop its part rows before cleanup."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if owner is not None and run_id is not None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (utc_timestamp(datetime.now(UTC)), run_id, owner),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise LeaseLostError()
            for entry in entries:
                connection.execute("DELETE FROM manifest WHERE path = ?", (entry.path,))
            if run_id is None:
                connection.execute(
                    """
                    UPDATE backfill_manifest SET
                        status = 'FAILED', checksum_verified = 0, error_code = ?
                    WHERE file_key = ? AND status = 'COMPLETE'
                    """,
                    (error_code, file_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE backfill_manifest SET
                        status = 'FAILED', checksum_verified = 0, error_code = ?
                    WHERE file_key = ? AND run_id = ? AND status = 'COMPLETE'
                    """,
                    (error_code, file_key, run_id),
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

    # -- Durable job ledger ----------------------------------------------

    def claim_job_run(self, record: JobRunRecord) -> tuple[bool, JobRunRecord | None, bool]:
        """Atomically claim ``(job_type, window_start)`` or reclaim a stale lease.

        Returns ``(claimed, run, reclaimed)``. A terminal row (``DONE``,
        ``FAILED``, ``MISSED``, ``SKIPPED``) is never replayed; a ``RUNNING``
        row is reclaimed only once its lease has expired, keeping the same
        ``run_id`` so checkpointed children can resume.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                (record.job_type, record.window_start),
            ).fetchone()
            if row is not None:
                existing = _row_to_job_run(row)
                stale = existing.lease_expires_at <= record.heartbeat_at
                if existing.status == "RUNNING" and stale:
                    connection.execute(
                        """
                        UPDATE job_runs SET
                            status = 'RUNNING', trigger_source = ?, started_at = ?,
                            finished_at = NULL, error_code = NULL, message = NULL,
                            lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
                            period = COALESCE(period, ?)
                        WHERE run_id = ?
                        """,
                        (
                            record.trigger_source,
                            record.started_at,
                            record.lease_owner,
                            record.lease_expires_at,
                            record.heartbeat_at,
                            record.period,
                            existing.run_id,
                        ),
                    )
                    updated = connection.execute(
                        "SELECT * FROM job_runs WHERE run_id = ?", (existing.run_id,)
                    ).fetchone()
                    connection.commit()
                    return True, _row_to_job_run(updated), True
                connection.commit()
                return False, existing, False
            try:
                connection.execute(
                    """
                    INSERT INTO job_runs (
                        run_id, job_type, window_start, status, trigger_source,
                        started_at, finished_at, error_code, message, lease_owner,
                        lease_expires_at, heartbeat_at, source, metrics_only, provider,
                        symbol, period
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.job_type,
                        record.window_start,
                        record.status,
                        record.trigger_source,
                        record.started_at,
                        record.finished_at,
                        record.error_code,
                        record.message,
                        record.lease_owner,
                        record.lease_expires_at,
                        record.heartbeat_at,
                        record.source,
                        int(record.metrics_only),
                        record.provider,
                        record.symbol,
                        record.period,
                    ),
                )
                connection.commit()
                return True, record, False
            except sqlite3.IntegrityError:
                connection.rollback()
                duplicate = connection.execute(
                    "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                    (record.job_type, record.window_start),
                ).fetchone()
                return False, _row_to_job_run(duplicate) if duplicate is not None else None, False
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"job claim failed: {exc}") from exc
        finally:
            connection.close()

    def finish_job_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: str,
        error_code: str | None = None,
        message: str | None = None,
        lease_owner: str | None = None,
    ) -> bool:
        owner_clause = " AND lease_owner = ?" if lease_owner is not None else ""
        params: list[object] = [status, finished_at, error_code, message, run_id]
        if lease_owner is not None:
            params.append(lease_owner)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"""
                UPDATE job_runs SET status = ?, finished_at = ?, error_code = ?, message = ?
                WHERE run_id = ? AND status = 'RUNNING'{owner_clause}
                """,
                tuple(params),
            )
        return cursor.rowcount > 0

    def fail_stale_job_runs(self, *, job_type: str, now: str) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE job_runs SET
                    status = 'FAILED', finished_at = ?, error_code = 'LEASE_EXPIRED',
                    message = 'stale RUNNING scan lease expired'
                WHERE job_type = ? AND status = 'RUNNING' AND lease_expires_at <= ?
                """,
                (now, job_type, now),
            )
        return cursor.rowcount

    def delete_job_run(self, run_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM job_runs WHERE run_id = ? AND status = 'RUNNING'", (run_id,)
            )
        return cursor.rowcount > 0

    def renew_job_lease(
        self, run_id: str, *, owner: str, heartbeat_at: str, lease_expires_at: str
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE job_runs SET heartbeat_at = ?, lease_expires_at = ?
                WHERE run_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (heartbeat_at, lease_expires_at, run_id, owner),
            )
        return cursor.rowcount > 0

    def get_job_run(self, run_id: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def get_job_run_by_window(self, job_type: str, window_start: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                (job_type, window_start),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def latest_job_run(self, job_type: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT * FROM job_runs WHERE job_type = ?
                ORDER BY heartbeat_at DESC, rowid DESC LIMIT 1
                """,
                (job_type,),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def list_job_runs(
        self, job_type: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[JobRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where = " WHERE job_type = ?" if job_type is not None else ""
        params: tuple[object, ...] = (
            (job_type, limit, offset) if job_type is not None else (limit, offset)
        )
        try:
            rows = connection.execute(
                f"SELECT * FROM job_runs{where} ORDER BY heartbeat_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_job_run(row) for row in rows)

    def count_job_runs(self, job_type: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where = " WHERE job_type = ?" if job_type is not None else ""
        params: tuple[object, ...] = (job_type,) if job_type is not None else ()
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM job_runs{where}", params).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def list_stale_job_runs(self, *, now: str) -> tuple[JobRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM job_runs "
                "WHERE status = 'RUNNING' AND lease_expires_at <= ? "
                "ORDER BY heartbeat_at DESC, rowid DESC",
                (now,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_job_run(row) for row in rows)

    def acquire_job_lock(
        self, lock_name: str, owner: str, *, acquired_at: str, expires_at: str
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO job_locks (lock_name, owner, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                WHERE job_locks.expires_at <= excluded.acquired_at
                   OR job_locks.owner = excluded.owner
                """,
                (lock_name, owner, acquired_at, expires_at),
            )
        return cursor.rowcount == 1

    def release_job_lock(self, lock_name: str, owner: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM job_locks WHERE lock_name = ? AND owner = ?",
                (lock_name, owner),
            )
        return cursor.rowcount > 0

    def get_job_lock(self, lock_name: str) -> JobLockRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_locks WHERE lock_name = ?", (lock_name,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_lock(row) if row is not None else None

    def upsert_scan_symbol(self, record: ScanSymbolRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO scan_symbols (
                    scan_id, symbol, stage, started_at, finished_at, error_code,
                    error_message, failed_stage, report_id, data_quality, llm_status,
                    llm_tokens, cost_usd, cost_unpriced
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id, symbol) DO UPDATE SET
                    stage = excluded.stage,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    failed_stage = excluded.failed_stage,
                    report_id = excluded.report_id,
                    data_quality = excluded.data_quality,
                    llm_status = excluded.llm_status,
                    llm_tokens = excluded.llm_tokens,
                    cost_usd = excluded.cost_usd,
                    cost_unpriced = excluded.cost_unpriced
                """,
                (
                    record.scan_id,
                    record.symbol,
                    record.stage,
                    record.started_at,
                    record.finished_at,
                    record.error_code,
                    record.error_message,
                    record.failed_stage,
                    record.report_id,
                    record.data_quality,
                    record.llm_status,
                    record.llm_tokens,
                    record.cost_usd,
                    int(record.cost_unpriced),
                ),
            )

    def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scan_symbols WHERE scan_id = ? ORDER BY symbol", (scan_id,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan_symbol(row) for row in rows)

    # -- LLM decision ledger ---------------------------------------------

    def save_llm_call(self, record: LLMCallRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_llm_call(connection, record)

    def _insert_llm_call(self, connection: sqlite3.Connection, record: LLMCallRecord) -> None:
        connection.execute(
            """
            INSERT INTO llm_calls (
                call_id, decision_id, role, provider, model, prompt_id,
                prompt_version, prompt_hash, input_hash, attempt, request_id,
                status, raw_output_hash, usage_json, latency_ms, error_code, created_at,
                system_prompt_id, system_prompt_version, system_prompt_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(call_id) DO NOTHING
            """,
            (
                record.call_id,
                record.decision_id,
                record.role,
                record.provider,
                record.model,
                record.prompt_id,
                record.prompt_version,
                record.prompt_hash,
                record.input_hash,
                record.attempt,
                record.request_id,
                record.status,
                record.raw_output_hash,
                record.usage_json,
                record.latency_ms,
                record.error_code,
                record.created_at,
                record.system_prompt_id,
                record.system_prompt_version,
                record.system_prompt_hash,
            ),
        )

    def list_llm_calls(
        self, *, decision_id: str | None = None, role: str | None = None
    ) -> tuple[LLMCallRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        clauses: list[str] = []
        params: list[object] = []
        if decision_id is not None:
            clauses.append("decision_id = ?")
            params.append(decision_id)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = connection.execute(
                f"SELECT * FROM llm_calls {where} ORDER BY created_at, attempt", tuple(params)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_llm_call(row) for row in rows)

    def save_decision(self, record: DecisionRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, role, decision_type, validation_status,
                    raw_output_hash, persisted_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO NOTHING
                """,
                (
                    record.decision_id,
                    record.role,
                    record.decision_type,
                    record.validation_status,
                    record.raw_output_hash,
                    record.persisted_at,
                    record.payload_json,
                ),
            )

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_decision(row) if row is not None else None

    def list_decisions(self, *, role: str | None = None) -> tuple[DecisionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if role is None:
            query = "SELECT * FROM decisions ORDER BY persisted_at"
            params: tuple[object, ...] = ()
        else:
            query = "SELECT * FROM decisions WHERE role = ? ORDER BY persisted_at"
            params = (role,)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_decision(row) for row in rows)

    # -- Prompt templates and immutable versions (ADR-0009) --------------

    def save_prompt_template(self, record: PromptTemplateRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO prompt_templates
                    (prompt_id, role, active_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(prompt_id) DO UPDATE SET
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (
                    record.prompt_id,
                    record.role,
                    record.active_version,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def get_prompt_template(self, prompt_id: str) -> PromptTemplateRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM prompt_templates WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_prompt_template(row)

    def list_prompt_templates(self) -> tuple[PromptTemplateRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM prompt_templates ORDER BY prompt_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_prompt_template(row) for row in rows)

    def save_prompt_version(self, record: PromptVersionRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO prompt_versions (
                    prompt_id, version, body, body_hash, is_active, created_at,
                    system_prompt_id, system_prompt_version, system_body,
                    system_body_hash, required_variables_json, knowledge_artifact_hash,
                    status, author, reason, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.prompt_id,
                    record.version,
                    record.body,
                    record.body_hash,
                    int(record.is_active),
                    record.created_at,
                    record.system_prompt_id,
                    record.system_prompt_version,
                    record.system_body,
                    record.system_body_hash,
                    record.required_variables_json,
                    record.knowledge_artifact_hash,
                    record.status,
                    record.author,
                    record.reason,
                    int(record.is_default),
                ),
            )

    def get_prompt_version(self, prompt_id: str, version: int) -> PromptVersionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_prompt_version(row)

    def list_prompt_versions(self, prompt_id: str) -> tuple[PromptVersionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version",
                (prompt_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_prompt_version(row) for row in rows)

    def activate_prompt_version(self, prompt_id: str, version: int, *, activated_at: str) -> bool:
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM prompt_versions WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                """
                UPDATE prompt_versions
                SET is_active = 0, status = ?
                WHERE prompt_id = ? AND version <> ? AND is_active = 1
                """,
                (
                    PromptStatus.SUPERSEDED.value,
                    prompt_id,
                    version,
                ),
            )
            connection.execute(
                "UPDATE prompt_versions SET is_active = 1, status = ? "
                "WHERE prompt_id = ? AND version = ?",
                (PromptStatus.ACTIVE.value, prompt_id, version),
            )
            connection.execute(
                "UPDATE prompt_templates SET active_version = ?, updated_at = ? "
                "WHERE prompt_id = ?",
                (version, activated_at, prompt_id),
            )
            return True

    # -- Budget ledger ---------------------------------------------------

    def _insert_budget_record(self, connection: sqlite3.Connection, record: BudgetRecord) -> int:
        cursor = connection.execute(
            """
            INSERT INTO budget_ledger (
                record_id, call_id, decision_id, occurred_at, provider, model,
                cost_usd, price_version, unpriced, budget_state, period_day,
                period_week, period_month, role, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO NOTHING
            """,
            (
                record.record_id,
                record.call_id,
                record.decision_id,
                record.occurred_at,
                record.provider,
                record.model,
                record.cost_usd,
                record.price_version,
                int(record.unpriced),
                record.budget_state,
                record.period_day,
                record.period_week,
                record.period_month,
                record.role,
                record.payload_json,
            ),
        )
        return cursor.rowcount

    def save_budget_record(self, record: BudgetRecord) -> bool:
        """Insert one ledger row; return whether it was actually written.

        ``BEGIN IMMEDIATE`` serializes concurrent writers. A duplicate
        ``record_id`` is an idempotent no-op and reports ``False``.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._insert_budget_record(connection, record) > 0
            connection.execute("COMMIT")
            return inserted
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _allocate_call_id(self, connection: sqlite3.Connection, base: str) -> str:
        """A call id that does not collide in either ledger table (M2/M3)."""
        candidate = base
        while self._call_id_exists(connection, candidate):
            candidate = f"{base}-{uuid.uuid4().hex[:8].upper()}"
        return candidate

    @staticmethod
    def _call_id_exists(connection: sqlite3.Connection, call_id: str) -> bool:
        for table in ("llm_calls", "budget_ledger"):
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE call_id = ? LIMIT 1", (call_id,)
            ).fetchone()
            if row is not None:
                return True
        return False

    @staticmethod
    def _rewrite_budget_payload(
        payload_json: str, *, call_id: str, record_id: str, budget_state: str
    ) -> str:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return payload_json
        if not isinstance(payload, dict):
            return payload_json
        payload["call_id"] = call_id
        payload["record_id"] = record_id
        payload["budget_state"] = budget_state
        return json.dumps(payload, sort_keys=True)

    def save_call_and_budget(
        self,
        call: LLMCallRecord,
        budget: BudgetRecord,
        compute: Callable[[dict[str, dict[str, Any]]], tuple[str, AuditEventDraft | None]],
    ) -> tuple[LLMCallRecord, BudgetRecord]:
        """Atomically write the call row, budget row, and threshold audit (M2).

        The whole read-compute-write cycle runs under one ``BEGIN IMMEDIATE``:
        the prior totals, the state/crossing decision, both inserts, and the
        audit commit together. Cross-process writers therefore cannot split the
        rows or miss a crossing.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            final_call = call
            final_budget = budget
            if self._call_id_exists(connection, call.call_id):
                allocated = self._allocate_call_id(connection, call.call_id)
                final_call = replace(call, call_id=allocated)
                final_budget = replace(
                    budget,
                    call_id=allocated,
                    record_id=budget_record_id(allocated),
                )
            else:
                allocated = call.call_id
            prior = self._budget_totals_for(
                connection,
                day=final_budget.period_day,
                week=final_budget.period_week,
                month=final_budget.period_month,
            )
            state, audit = compute(prior)
            final_budget = replace(
                final_budget,
                budget_state=state,
                payload_json=self._rewrite_budget_payload(
                    final_budget.payload_json,
                    call_id=allocated,
                    record_id=final_budget.record_id,
                    budget_state=state,
                ),
            )
            self._insert_llm_call(connection, final_call)
            inserted_budget = self._insert_budget_record(connection, final_budget)
            if inserted_budget <= 0:
                # A colliding ``record_id`` makes the budget insert a no-op.
                # Raise inside the transaction so the call row rolls back too;
                # returning here would hand the caller a phantom entry.
                raise BudgetRecordConflictError(
                    f"budget record {final_budget.record_id!r} was not persisted"
                )
            if audit is not None:
                # The id may have been reallocated after the caller built the
                # draft; point the audit at the row that actually committed.
                self._insert_audit_event(
                    connection,
                    replace(
                        audit,
                        subject={**audit.subject, "id": final_budget.record_id},
                    ),
                )
            connection.execute("COMMIT")
            return final_call, final_budget
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_budget_records(self, *, decision_id: str | None = None) -> tuple[BudgetRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if decision_id is None:
            query = "SELECT * FROM budget_ledger ORDER BY occurred_at"
            params: tuple[object, ...] = ()
        else:
            query = "SELECT * FROM budget_ledger WHERE decision_id = ? ORDER BY occurred_at"
            params = (decision_id,)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_budget(row) for row in rows)

    def last_budget_record(self) -> BudgetRecord | None:
        """The most recent ledger row, without loading the table (M24)."""
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM budget_ledger ORDER BY occurred_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_budget(row)

    def list_budget_records_for_period(self, period: str, key: str) -> tuple[BudgetRecord, ...]:
        """Rows bucketed in one period, filtered in SQL rather than Python (M24)."""
        columns = {"day": "period_day", "week": "period_week", "month": "period_month"}
        column = columns.get(period)
        if column is None:
            raise ValueError("period must be day, week, or month")
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM budget_ledger WHERE {column} = ? ORDER BY occurred_at",
                (key,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_budget(row) for row in rows)

    def budget_totals(self, *, day: str, week: str, month: str) -> dict[str, dict[str, Any]]:
        connection = self._connect_read_only()
        if connection is None:
            return _zero_budget_totals()
        try:
            return self._budget_totals_for(connection, day=day, week=week, month=month)
        finally:
            connection.close()

    @staticmethod
    def _budget_totals_for(
        connection: sqlite3.Connection, *, day: str, week: str, month: str
    ) -> dict[str, dict[str, Any]]:
        columns = {"day": "period_day", "week": "period_week", "month": "period_month"}
        keys = {"day": day, "week": week, "month": month}
        quantum = Decimal("0.00000001")
        totals: dict[str, dict[str, Any]] = {}
        for name, column in columns.items():
            rows = connection.execute(
                f"""
                SELECT cost_usd, unpriced
                FROM budget_ledger WHERE {column} = ?
                """,
                (keys[name],),
            ).fetchall()
            # Exact Decimal accumulation: SQLite REAL sums drift at 8 dp.
            spend = Decimal("0")
            unpriced_count = 0
            for row in rows:
                try:
                    spend += Decimal(str(row[0]))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if int(row[1]) == 1:
                    unpriced_count += 1
            totals[name] = {
                "spend_usd": format(spend.quantize(quantum, rounding=ROUND_HALF_UP), "f"),
                "call_count": len(rows),
                "unpriced_count": unpriced_count,
            }
        return totals

    def get_budget_settings(self) -> BudgetSettingsRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM budget_settings WHERE id = 'default'"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return BudgetSettingsRecord(
            id=row["id"],
            daily_usd=row["daily_usd"],
            weekly_usd=row["weekly_usd"],
            monthly_usd=row["monthly_usd"],
            warn_pct=float(row["warn_pct"]),
            updated_at=row["updated_at"],
        )

    def save_budget_settings(self, record: BudgetSettingsRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO budget_settings
                    (id, daily_usd, weekly_usd, monthly_usd, warn_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    daily_usd = excluded.daily_usd,
                    weekly_usd = excluded.weekly_usd,
                    monthly_usd = excluded.monthly_usd,
                    warn_pct = excluded.warn_pct,
                    updated_at = excluded.updated_at
                """,
                (
                    "default",
                    record.daily_usd,
                    record.weekly_usd,
                    record.monthly_usd,
                    record.warn_pct,
                    record.updated_at,
                ),
            )

    # -- Provider capability probes --------------------------------------

    def save_provider_capability(self, record: ProviderCapabilityRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO provider_capabilities (
                    provider, model, strict_schema, degraded, max_output_tokens,
                    status, error_code, notes, latency_ms, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    model = excluded.model,
                    strict_schema = excluded.strict_schema,
                    degraded = excluded.degraded,
                    max_output_tokens = excluded.max_output_tokens,
                    status = excluded.status,
                    error_code = excluded.error_code,
                    notes = excluded.notes,
                    latency_ms = excluded.latency_ms,
                    checked_at = excluded.checked_at
                """,
                (
                    record.provider,
                    record.model,
                    int(record.strict_schema),
                    int(record.degraded),
                    record.max_output_tokens,
                    record.status,
                    record.error_code,
                    record.notes,
                    record.latency_ms,
                    record.checked_at,
                ),
            )

    def get_provider_capability(self, provider: str) -> ProviderCapabilityRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM provider_capabilities WHERE provider = ?", (provider,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_provider_capability(row)

    def list_provider_capabilities(self) -> tuple[ProviderCapabilityRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM provider_capabilities ORDER BY provider"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_provider_capability(row) for row in rows)

    # -- Opportunity ledger ----------------------------------------------

    def save_opportunity(
        self, record: OpportunityRecord, event: OpportunityEventRecord | None = None
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO opportunities (
                    opportunity_id, symbol, state, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id) DO NOTHING
                """,
                (
                    record.opportunity_id,
                    record.symbol,
                    record.state,
                    record.created_at,
                    record.updated_at,
                    record.payload_json,
                ),
            )
            if event is not None:
                # The CREATED row is part of the same unit: the timeline must not
                # lose the creation event once a transition event exists.
                connection.execute(
                    """
                    INSERT OR IGNORE INTO opportunity_events (
                        opportunity_id, seq, at, from_state, to_state, reason_code, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.opportunity_id,
                        event.seq,
                        event.at,
                        event.from_state,
                        event.to_state,
                        event.reason_code,
                        event.note,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition_opportunity(
        self, record: OpportunityRecord, event: OpportunityEventRecord
    ) -> bool:
        # OPEN may move to any terminal state; TRIGGERED may only be invalidated
        # (a human reject or NO_ENTRY re-check). The domain transition() is the
        # authority; this condition pins the same rule in one atomic statement.
        allowed_from: tuple[str, ...]
        if record.state == "INVALIDATED":
            allowed_from = ("OPEN", "TRIGGERED")
        elif record.state in {"TRIGGERED", "EXPIRED"}:
            allowed_from = ("OPEN",)
        else:
            return False
        placeholders = ", ".join("?" for _ in allowed_from)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"""
                UPDATE opportunities SET state = ?, updated_at = ?, payload_json = ?
                WHERE opportunity_id = ? AND state IN ({placeholders})
                """,
                (
                    record.state,
                    record.updated_at,
                    record.payload_json,
                    record.opportunity_id,
                    *allowed_from,
                ),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                """
                INSERT INTO opportunity_events (
                    opportunity_id, seq, at, from_state, to_state, reason_code, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.opportunity_id,
                    event.seq,
                    event.at,
                    event.from_state,
                    event.to_state,
                    event.reason_code,
                    event.note,
                ),
            )
        return True

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_opportunity(row) if row is not None else None

    def list_opportunities(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[OpportunityRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if state is None:
            query = "SELECT * FROM opportunities ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params: tuple[object, ...] = (limit, offset)
        else:
            query = (
                "SELECT * FROM opportunities WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params = (state, limit, offset)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_opportunity(row) for row in rows)

    def count_opportunities(self, *, state: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if state is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM opportunities").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM opportunities WHERE state = ?", (state,)
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def list_opportunity_events(self, opportunity_id: str) -> tuple[OpportunityEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM opportunity_events WHERE opportunity_id = ? ORDER BY seq",
                (opportunity_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_opportunity_event(row) for row in rows)

    # -- Triggers and risk verdicts --------------------------------------

    def save_trigger_event(self, record: TriggerEventRecord) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO trigger_events (
                    trigger_id, opportunity_id, symbol, status, idempotency_key,
                    triggered_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trigger_id,
                    record.opportunity_id,
                    record.symbol,
                    record.status,
                    record.idempotency_key,
                    record.triggered_at,
                    record.payload_json,
                ),
            )
        return cursor.rowcount == 1

    def update_trigger_event(self, record: TriggerEventRecord) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE trigger_events SET status = ?, payload_json = ?
                WHERE trigger_id = ?
                """,
                (record.status, record.payload_json, record.trigger_id),
            )
        return cursor.rowcount == 1

    def get_trigger_event(self, trigger_id: str) -> TriggerEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trigger_events WHERE trigger_id = ?", (trigger_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trigger_event(row) if row is not None else None

    def get_trigger_by_idempotency(self, idempotency_key: str) -> TriggerEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trigger_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trigger_event(row) if row is not None else None

    def list_trigger_events(
        self, *, opportunity_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[TriggerEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if opportunity_id is None:
            query = "SELECT * FROM trigger_events ORDER BY triggered_at DESC LIMIT ? OFFSET ?"
            params: tuple[object, ...] = (limit, offset)
        else:
            query = (
                "SELECT * FROM trigger_events WHERE opportunity_id = ? "
                "ORDER BY triggered_at DESC LIMIT ? OFFSET ?"
            )
            params = (opportunity_id, limit, offset)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_trigger_event(row) for row in rows)

    def count_trigger_events(self, *, opportunity_id: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if opportunity_id is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM trigger_events").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM trigger_events WHERE opportunity_id = ?",
                    (opportunity_id,),
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def save_risk_verdict(self, record: RiskVerdictRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO risk_verdicts (
                    risk_verdict_id, trigger_id, decision_id, verdict, binding_cap,
                    computed_at, expires_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(risk_verdict_id) DO NOTHING
                """,
                (
                    record.risk_verdict_id,
                    record.trigger_id,
                    record.decision_id,
                    record.verdict,
                    record.binding_cap,
                    record.computed_at,
                    record.expires_at,
                    record.payload_json,
                ),
            )

    def get_risk_verdict(self, risk_verdict_id: str) -> RiskVerdictRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM risk_verdicts WHERE risk_verdict_id = ?",
                (risk_verdict_id,),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_risk_verdict(row) if row is not None else None

    # -- Paper OMS -------------------------------------------------------

    def save_order_fill_position(
        self,
        *,
        order: OrderRecord,
        fill: FillRecord,
        position: PositionRecord,
        account: PaperAccountRecord,
        balance_delta: str = "0",
        audit: AuditEventDraft | None = None,
    ) -> OmsCommit:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit_record = (
                self._insert_audit_event(connection, audit) if audit is not None else None
            )
            # Read the account inside the write transaction: an absolute balance
            # computed by a caller would lose one of two concurrent debits.
            existing = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (account.account_id,)
            ).fetchone()
            if existing is not None:
                base = Decimal(str(existing["balance"]))
                initial_balance = str(existing["initial_balance"])
                environment = str(existing["environment"])
                currency = str(existing["currency"])
            else:
                base = Decimal(account.initial_balance)
                initial_balance = account.initial_balance
                environment = account.environment
                currency = account.currency
            balance = (base + Decimal(balance_delta)).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, client_order_id, environment, symbol, status,
                    risk_verdict_id, idempotency_key, created_at, updated_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.client_order_id,
                    order.environment,
                    order.symbol,
                    order.status,
                    order.risk_verdict_id,
                    order.idempotency_key,
                    order.created_at,
                    order.updated_at,
                    order.payload_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO fills (
                    fill_id, order_id, environment, symbol, filled_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.environment,
                    fill.symbol,
                    fill.filled_at,
                    fill.payload_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO positions (
                    position_id, account_id, environment, symbol, state,
                    opened_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    position.account_id,
                    position.environment,
                    position.symbol,
                    position.state,
                    position.opened_at,
                    position.payload_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, environment, currency, initial_balance, balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (
                    account.account_id,
                    environment,
                    currency,
                    initial_balance,
                    str(balance),
                    account.updated_at,
                ),
            )
            connection.commit()
            committed_account = PaperAccountRecord(
                account_id=account.account_id,
                environment=environment,
                currency=currency,
                initial_balance=initial_balance,
                balance=str(balance),
                updated_at=account.updated_at,
            )
            return OmsCommit(account=committed_account, audit=audit_record)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_order(self, order_id: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def get_order_by_idempotency(self, idempotency_key: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def get_order_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def list_orders(self, *, limit: int = 100, offset: int = 0) -> tuple[OrderRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_order(row) for row in rows)

    def count_orders(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM orders").fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def list_fills(self, *, limit: int = 100, offset: int = 0) -> tuple[FillRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_fill(row) for row in rows)

    def count_fills(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM fills").fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def get_fill_by_order_id(self, order_id: str) -> FillRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM fills WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_fill(row) if row is not None else None

    def list_positions(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[PositionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if state is None:
            query = "SELECT * FROM positions ORDER BY opened_at DESC LIMIT ? OFFSET ?"
            params: tuple[object, ...] = (limit, offset)
        else:
            query = (
                "SELECT * FROM positions WHERE state = ? ORDER BY opened_at DESC LIMIT ? OFFSET ?"
            )
            params = (state, limit, offset)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_position(row) for row in rows)

    def count_positions(self, *, state: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if state is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM positions").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM positions WHERE state = ?", (state,)
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def get_position(self, position_id: str) -> PositionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_position(row) if row is not None else None

    def get_paper_account(self, account_id: str) -> PaperAccountRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_paper_account(row) if row is not None else None

    def save_paper_account(self, record: PaperAccountRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, environment, currency, initial_balance, balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (
                    record.account_id,
                    record.environment,
                    record.currency,
                    record.initial_balance,
                    record.balance,
                    record.updated_at,
                ),
            )

    # -- Audit and system state ------------------------------------------

    def save_audit_event(self, record: AuditEventRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, seq, event_type, created_at, previous_hash,
                    event_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    record.seq,
                    record.event_type,
                    record.created_at,
                    record.previous_hash,
                    record.event_hash,
                    record.payload_json,
                ),
            )

    def append_audit_event(self, draft: AuditEventDraft) -> AuditEventRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._insert_audit_event(connection, draft)
            connection.commit()
            return record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_audit_event(
        self, connection: sqlite3.Connection, draft: AuditEventDraft
    ) -> AuditEventRecord:
        previous = connection.execute(
            "SELECT seq, event_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        entry = build_audit_entry(
            previous_seq=None if previous is None else int(previous["seq"]),
            previous_hash=None if previous is None else str(previous["event_hash"]),
            event_type=draft.event_type,
            actor=draft.actor,
            subject=draft.subject,
            payload=draft.payload,
            created_at=draft.created_at,
        )
        record = AuditEventRecord(
            event_id=entry.event_id,
            seq=entry.seq,
            event_type=draft.event_type,
            created_at=draft.created_at,
            previous_hash=entry.previous_hash,
            event_hash=entry.event_hash,
            payload_json=entry.payload_json,
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, seq, event_type, created_at, previous_hash,
                event_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id,
                record.seq,
                record.event_type,
                record.created_at,
                record.previous_hash,
                record.event_hash,
                record.payload_json,
            ),
        )
        return record

    def list_audit_events(self, *, limit: int = 100) -> tuple[AuditEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_audit(row) for row in rows)

    def last_audit_event(self) -> AuditEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_audit(row) if row is not None else None

    def get_system_state(self, key: str) -> str | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        return str(row["value"]) if row is not None else None

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at),
            )


def _row_to_llm_call(row: sqlite3.Row) -> LLMCallRecord:
    keys = row.keys()
    return LLMCallRecord(
        call_id=row["call_id"],
        decision_id=row["decision_id"],
        role=row["role"],
        provider=row["provider"],
        model=row["model"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
        prompt_hash=row["prompt_hash"],
        input_hash=row["input_hash"],
        attempt=row["attempt"],
        request_id=row["request_id"],
        status=row["status"],
        raw_output_hash=row["raw_output_hash"],
        usage_json=row["usage_json"],
        latency_ms=row["latency_ms"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        system_prompt_id=row["system_prompt_id"] if "system_prompt_id" in keys else "",
        system_prompt_version=(
            row["system_prompt_version"] if "system_prompt_version" in keys else 1
        ),
        system_prompt_hash=row["system_prompt_hash"] if "system_prompt_hash" in keys else "",
    )


def _row_to_prompt_template(row: sqlite3.Row) -> PromptTemplateRecord:
    return PromptTemplateRecord(
        prompt_id=row["prompt_id"],
        role=PromptRole(row["role"]),
        active_version=row["active_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_prompt_version(row: sqlite3.Row) -> PromptVersionRecord:
    keys = row.keys()
    return PromptVersionRecord(
        prompt_id=row["prompt_id"],
        version=row["version"],
        body=row["body"],
        body_hash=row["body_hash"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        system_prompt_id=row["system_prompt_id"] if "system_prompt_id" in keys else "",
        system_prompt_version=(
            row["system_prompt_version"] if "system_prompt_version" in keys else 1
        ),
        system_body=row["system_body"] if "system_body" in keys else "",
        system_body_hash=row["system_body_hash"] if "system_body_hash" in keys else "",
        required_variables_json=(
            row["required_variables_json"] if "required_variables_json" in keys else "[]"
        ),
        knowledge_artifact_hash=(
            row["knowledge_artifact_hash"] if "knowledge_artifact_hash" in keys else None
        ),
        status=(PromptStatus(row["status"]) if "status" in keys else PromptStatus.ACTIVE),
        author=row["author"] if "author" in keys else "",
        reason=row["reason"] if "reason" in keys else "",
        is_default=bool(row["is_default"]) if "is_default" in keys else False,
    )


def _row_to_decision(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=row["decision_id"],
        role=row["role"],
        decision_type=row["decision_type"],
        validation_status=row["validation_status"],
        raw_output_hash=row["raw_output_hash"],
        persisted_at=row["persisted_at"],
        payload_json=row["payload_json"],
    )


def _row_to_budget(row: sqlite3.Row) -> BudgetRecord:
    keys = row.keys()
    return BudgetRecord(
        record_id=row["record_id"],
        call_id=row["call_id"],
        decision_id=row["decision_id"],
        occurred_at=row["occurred_at"],
        provider=row["provider"],
        model=row["model"],
        cost_usd=row["cost_usd"],
        price_version=row["price_version"],
        unpriced=bool(row["unpriced"]),
        budget_state=row["budget_state"],
        period_day=row["period_day"],
        payload_json=row["payload_json"],
        period_week=row["period_week"] if "period_week" in keys else "",
        period_month=row["period_month"] if "period_month" in keys else "",
        role=row["role"] if "role" in keys else "",
    )


def _row_to_provider_capability(row: sqlite3.Row) -> ProviderCapabilityRecord:
    return ProviderCapabilityRecord(
        provider=row["provider"],
        model=row["model"],
        strict_schema=bool(row["strict_schema"]),
        degraded=bool(row["degraded"]),
        max_output_tokens=row["max_output_tokens"],
        status=row["status"],
        error_code=row["error_code"],
        notes=row["notes"],
        latency_ms=row["latency_ms"],
        checked_at=row["checked_at"],
    )


def _row_to_opportunity(row: sqlite3.Row) -> OpportunityRecord:
    return OpportunityRecord(
        opportunity_id=row["opportunity_id"],
        symbol=row["symbol"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload_json=row["payload_json"],
    )


def _row_to_opportunity_event(row: sqlite3.Row) -> OpportunityEventRecord:
    return OpportunityEventRecord(
        opportunity_id=row["opportunity_id"],
        seq=row["seq"],
        at=row["at"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        reason_code=row["reason_code"],
        note=row["note"],
    )


def _row_to_trigger_event(row: sqlite3.Row) -> TriggerEventRecord:
    return TriggerEventRecord(
        trigger_id=row["trigger_id"],
        opportunity_id=row["opportunity_id"],
        symbol=row["symbol"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        triggered_at=row["triggered_at"],
        payload_json=row["payload_json"],
    )


def _row_to_risk_verdict(row: sqlite3.Row) -> RiskVerdictRecord:
    return RiskVerdictRecord(
        risk_verdict_id=row["risk_verdict_id"],
        trigger_id=row["trigger_id"],
        decision_id=row["decision_id"],
        verdict=row["verdict"],
        binding_cap=row["binding_cap"],
        computed_at=row["computed_at"],
        expires_at=row["expires_at"],
        payload_json=row["payload_json"],
    )


def _row_to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        order_id=row["order_id"],
        client_order_id=row["client_order_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        status=row["status"],
        risk_verdict_id=row["risk_verdict_id"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload_json=row["payload_json"],
    )


def _row_to_fill(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        fill_id=row["fill_id"],
        order_id=row["order_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        filled_at=row["filled_at"],
        payload_json=row["payload_json"],
    )


def _row_to_position(row: sqlite3.Row) -> PositionRecord:
    return PositionRecord(
        position_id=row["position_id"],
        account_id=row["account_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        state=row["state"],
        opened_at=row["opened_at"],
        payload_json=row["payload_json"],
    )


def _row_to_paper_account(row: sqlite3.Row) -> PaperAccountRecord:
    return PaperAccountRecord(
        account_id=row["account_id"],
        environment=row["environment"],
        currency=row["currency"],
        initial_balance=row["initial_balance"],
        balance=row["balance"],
        updated_at=row["updated_at"],
    )


def _row_to_audit(row: sqlite3.Row) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=row["event_id"],
        seq=row["seq"],
        event_type=row["event_type"],
        created_at=row["created_at"],
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
        payload_json=row["payload_json"],
    )
