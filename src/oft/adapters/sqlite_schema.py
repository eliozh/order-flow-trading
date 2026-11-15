"""Schema DDL, migrations, and database initialization for the SQLite store.

Owns ``_SCHEMA_SQL``, every migration helper, and ``initialize_database`` so
the concrete store stays a thin composition root and every table change lands
in one place.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from oft.adapters.sqlite_connections import configure_connection
from oft.config import Settings
from oft.domain.budget_periods import period_parts
from oft.domain.conditions import ConditionParamsError, normalize_timestamp
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

_LOGGER = logging.getLogger(__name__)


_SCHEMA_VERSION = 27
# ``PRAGMA journal_mode=WAL`` needs an exclusive lock and does not reliably
# consult the busy handler, so a concurrent initializer can still see
# SQLITE_BUSY with the timeout installed. Retry the switch briefly.
_WAL_SWITCH_ATTEMPTS = 5
_WAL_SWITCH_BACKOFF_SECONDS = 0.05

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


def _enum_check(values: tuple[str, ...]) -> str:
    """Render the ``IN`` list of an enum CHECK from its vocabulary tuple."""
    return ",".join(f"'{value}'" for value in values)


# v21 enum domains. Each tuple is exactly what the application writes: the
# ``status``/``state`` values come from the writer sites and the domain
# ``StrEnum`` values in ``oft.domain.oms``, plus the contract enums in
# ``docs/contracts/common.schema.json``. The CHECK text is generated from these
# tuples so the DDL cannot drift from the documented vocabulary, and the same
# definitions are attached to both a fresh database and the v20 -> v21 rebuild
# so the two schemas stay identical.
_LLM_CALL_STATUSES = ("OK", "ERROR")
_DECISION_VALIDATION_STATUSES = ("VALID", "REPAIRED", "REJECTED")
_TRIGGER_STATUSES = (
    "PENDING_RECHECK",
    "RECHECKED",
    "REJECTED",
    "BLOCKED",
    "ACCEPTED",
    "EXPIRED",
)
_ORDER_STATUSES = (
    "CREATED",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "REJECTED",
    "EXPIRED",
    "UNKNOWN",
)
_POSITION_STATES = ("OPEN", "CLOSED", "LIQUIDATED", "RECONCILING")

_LLM_CALLS_DEFINITION = f"""
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
    status TEXT NOT NULL CHECK (status IN ({_enum_check(_LLM_CALL_STATUSES)})),
    raw_output_hash TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    system_prompt_id TEXT NOT NULL DEFAULT '',
    system_prompt_version INTEGER NOT NULL DEFAULT 1,
    system_prompt_hash TEXT NOT NULL DEFAULT ''
"""
_DECISIONS_DEFINITION = f"""
    decision_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ({_enum_check(_DECISION_VALIDATION_STATUSES)})
    ),
    raw_output_hash TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
"""
_TRIGGER_EVENTS_DEFINITION = f"""
    trigger_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    symbol TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ({_enum_check(_TRIGGER_STATUSES)})
    ),
    idempotency_key TEXT NOT NULL UNIQUE,
    triggered_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL DEFAULT '',
    data_quality TEXT NOT NULL DEFAULT '',
    condition_count INTEGER NOT NULL DEFAULT 0
"""
_RISK_VERDICTS_DEFINITION = """
    risk_verdict_id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL REFERENCES trigger_events(trigger_id),
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('ALLOW','REDUCE','BLOCK')),
    binding_cap TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    spread_bps REAL,
    expected_slippage_bps REAL,
    daily_loss_usd TEXT
"""
_ORDERS_DEFINITION = f"""
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            {_enum_check(_ORDER_STATUSES)}
        )
    ),
    risk_verdict_id TEXT NOT NULL REFERENCES risk_verdicts(risk_verdict_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trigger_id TEXT,
    decision_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'ENTRY'
"""
_POSITIONS_DEFINITION = f"""
    position_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ({_enum_check(_POSITION_STATES)})
    ),
    opened_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
"""

# v22: the closed-trade journal and the append-only management timeline.
_TRADE_LOGS_DEFINITION = """
    trade_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL UNIQUE REFERENCES positions(position_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    net_pnl_usd TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    fill_id TEXT NOT NULL REFERENCES fills(fill_id),
    risk_verdict_id TEXT NOT NULL REFERENCES risk_verdicts(risk_verdict_id),
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    review_decision_id TEXT REFERENCES decisions(decision_id)
"""
# Exactly the actions the application writes to the management timeline.
_POSITION_EVENT_ACTIONS = ("FULL_CLOSE", "PARTIAL_CLOSE", "ADD", "FUNDING")
_POSITION_EVENTS_DEFINITION = f"""
    position_id TEXT NOT NULL REFERENCES positions(position_id),
    seq INTEGER NOT NULL CHECK (seq > 0),
    at TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ({_enum_check(_POSITION_EVENT_ACTIONS)})
    ),
    order_id TEXT,
    note TEXT NOT NULL,
    PRIMARY KEY (position_id, seq)
"""
_TRADE_LOGS_TABLE = f"CREATE TABLE IF NOT EXISTS trade_logs ({_TRADE_LOGS_DEFINITION});"
# The journal reads with ``ORDER BY closed_at DESC, trade_id ASC``; the
# composite indexes match that exact order, and the account index covers the
# foreign-key child column so a parent delete/update is not a full scan.
_TRADE_LOGS_INDEX_NAMES = (
    "trade_logs_closed",
    "trade_logs_symbol_closed",
    "trade_logs_account_id",
)
_TRADE_LOGS_INDEXES = (
    "DROP INDEX IF EXISTS trade_logs_closed_at;",
    "DROP INDEX IF EXISTS trade_logs_symbol;",
    "CREATE INDEX IF NOT EXISTS trade_logs_closed ON trade_logs(closed_at DESC, trade_id ASC);",
    "CREATE INDEX IF NOT EXISTS trade_logs_symbol_closed "
    "ON trade_logs(symbol, closed_at DESC, trade_id ASC);",
    "CREATE INDEX IF NOT EXISTS trade_logs_account_id ON trade_logs(account_id);",
    "CREATE INDEX IF NOT EXISTS trade_logs_review_decision ON trade_logs(review_decision_id);",
)
_POSITION_EVENTS_TABLE = (
    f"CREATE TABLE IF NOT EXISTS position_events ({_POSITION_EVENTS_DEFINITION});"
)
_POSITION_EVENTS_TRIGGER_NAMES = ("position_events_no_update", "position_events_no_delete")
_POSITION_EVENTS_TRIGGERS = (
    "DROP TRIGGER IF EXISTS position_events_no_update;",
    "CREATE TRIGGER position_events_no_update BEFORE UPDATE ON position_events "
    "BEGIN SELECT RAISE(ABORT, 'position_events is append-only'); END;",
    "DROP TRIGGER IF EXISTS position_events_no_delete;",
    "CREATE TRIGGER position_events_no_delete BEFORE DELETE ON position_events "
    "BEGIN SELECT RAISE(ABORT, 'position_events is append-only'); END;",
)
_V22_DDL = "\n".join(
    (_TRADE_LOGS_TABLE, *_TRADE_LOGS_INDEXES, _POSITION_EVENTS_TABLE, *_POSITION_EVENTS_TRIGGERS)
)

# v23: recorded funding settlements. The unique ``(position_id, settlement_time)``
# pair is the idempotency key: a replay or restart cannot accrue twice.
_FUNDING_ACCRUALS_DEFINITION = """
    accrual_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES positions(position_id),
    settlement_time TEXT NOT NULL,
    rate TEXT NOT NULL,
    mark_price TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    side TEXT NOT NULL,
    size TEXT NOT NULL,
    UNIQUE (position_id, settlement_time)
"""
_FUNDING_ACCRUALS_TABLE = (
    f"CREATE TABLE IF NOT EXISTS funding_accruals ({_FUNDING_ACCRUALS_DEFINITION});"
)
# No separate index: ``UNIQUE (position_id, settlement_time)`` already creates
# the covering autoindex, so a named duplicate would only add write amplification.
# The funding-history read filters reports by symbol and orders by
# ``(generated_at, report_id)``; this index is the exact covering shape.
_REPORTS_SYMBOL_GENERATED_INDEX = (
    "CREATE INDEX IF NOT EXISTS reports_symbol_generated "
    "ON reports(symbol, generated_at DESC, report_id);"
)
_V23_DDL = _FUNDING_ACCRUALS_TABLE

# Every column copied by the v21 rebuild, in table order.
_V21_REBUILDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "llm_calls",
        _LLM_CALLS_DEFINITION,
        (
            "call_id",
            "decision_id",
            "role",
            "provider",
            "model",
            "prompt_id",
            "prompt_version",
            "prompt_hash",
            "input_hash",
            "attempt",
            "request_id",
            "status",
            "raw_output_hash",
            "usage_json",
            "latency_ms",
            "error_code",
            "created_at",
            "system_prompt_id",
            "system_prompt_version",
            "system_prompt_hash",
        ),
    ),
    (
        "decisions",
        _DECISIONS_DEFINITION,
        (
            "decision_id",
            "role",
            "decision_type",
            "validation_status",
            "raw_output_hash",
            "persisted_at",
            "payload_json",
        ),
    ),
    (
        "trigger_events",
        _TRIGGER_EVENTS_DEFINITION,
        (
            "trigger_id",
            "opportunity_id",
            "symbol",
            "status",
            "idempotency_key",
            "triggered_at",
            "payload_json",
            "snapshot_hash",
            "as_of",
            "data_quality",
            "condition_count",
        ),
    ),
    (
        "risk_verdicts",
        _RISK_VERDICTS_DEFINITION,
        (
            "risk_verdict_id",
            "trigger_id",
            "decision_id",
            "verdict",
            "binding_cap",
            "computed_at",
            "expires_at",
            "payload_json",
            "spread_bps",
            "expected_slippage_bps",
            "daily_loss_usd",
        ),
    ),
    (
        "orders",
        _ORDERS_DEFINITION,
        (
            "order_id",
            "client_order_id",
            "environment",
            "symbol",
            "status",
            "risk_verdict_id",
            "idempotency_key",
            "created_at",
            "updated_at",
            "payload_json",
            "trigger_id",
            "decision_id",
            "purpose",
        ),
    ),
    (
        "positions",
        _POSITIONS_DEFINITION,
        (
            "position_id",
            "account_id",
            "environment",
            "symbol",
            "state",
            "opened_at",
            "payload_json",
        ),
    ),
)


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
_OPPORTUNITY_CONDITIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS opportunity_conditions (
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
"""
_OPPORTUNITY_CONDITIONS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS opportunity_conditions_opportunity "
    "ON opportunity_conditions(opportunity_id, kind, seq);",
    "CREATE INDEX IF NOT EXISTS opportunity_conditions_type ON opportunity_conditions(type);",
    "CREATE INDEX IF NOT EXISTS opportunity_conditions_evaluable "
    "ON opportunity_conditions(evaluable);",
    "CREATE INDEX IF NOT EXISTS opportunity_conditions_condition_id "
    "ON opportunity_conditions(condition_id);",
    "CREATE INDEX IF NOT EXISTS opportunities_state_updated ON opportunities(state, updated_at);",
    "CREATE INDEX IF NOT EXISTS opportunities_symbol_state ON opportunities(symbol, state);",
    "CREATE INDEX IF NOT EXISTS opportunities_expires_at ON opportunities(expires_at);",
    "CREATE INDEX IF NOT EXISTS opportunity_events_opportunity_at "
    "ON opportunity_events(opportunity_id, at);",
)
_OPPORTUNITY_CONDITIONS_DDL = _OPPORTUNITY_CONDITIONS_TABLE + "\n".join(
    _OPPORTUNITY_CONDITIONS_INDEXES
)

# v26: one row per persisted runtime setting. ``value_json`` keeps the typed
# value (a JSON string, number, boolean, or object) so later settings slices
# reuse the same table. The table and index live in one constant shared by the
# fresh schema and the v25 -> v26 migration so their shapes cannot drift.
_CONFIG_DEFINITION = """
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
"""
_V26_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS config ({_CONFIG_DEFINITION});",
    "CREATE INDEX IF NOT EXISTS config_updated_at ON config(updated_at);",
)
# v27: the reverse-scan lookup used to resolve the newest audit event of one
# type. Additive and idempotent; the fresh schema carries the same statement so
# both paths cannot drift.
_V27_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS audit_events_event_type_seq ON audit_events(event_type, seq DESC);",
)
_OPPORTUNITY_CONDITION_INDEX_NAMES = (
    "opportunity_conditions_opportunity",
    "opportunity_conditions_type",
    "opportunity_conditions_evaluable",
    "opportunity_conditions_condition_id",
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
    CREATE TABLE IF NOT EXISTS llm_calls ({_LLM_CALLS_DEFINITION});
    CREATE INDEX IF NOT EXISTS llm_calls_decision ON llm_calls(decision_id);
    CREATE TABLE IF NOT EXISTS decisions ({_DECISIONS_DEFINITION});
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
        payload_json TEXT NOT NULL,
        expires_at TEXT
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
    CREATE TABLE IF NOT EXISTS trigger_events ({_TRIGGER_EVENTS_DEFINITION});
    CREATE INDEX IF NOT EXISTS trigger_events_opportunity
        ON trigger_events(opportunity_id);
    CREATE INDEX IF NOT EXISTS trigger_events_opportunity_triggered
        ON trigger_events(opportunity_id, triggered_at);
    CREATE TABLE IF NOT EXISTS risk_verdicts ({_RISK_VERDICTS_DEFINITION});
    CREATE INDEX IF NOT EXISTS risk_verdicts_trigger_id ON risk_verdicts(trigger_id);
    -- ``decision_id`` is a foreign-key child column added in v21; index it so
    -- ``foreign_key_check`` and parent updates do not scan the table.
    CREATE INDEX IF NOT EXISTS risk_verdicts_decision_id ON risk_verdicts(decision_id);
    CREATE TABLE IF NOT EXISTS orders ({_ORDERS_DEFINITION});
    CREATE INDEX IF NOT EXISTS orders_risk_verdict_id ON orders(risk_verdict_id);
    -- D6: at most one ENTRY order per trigger, enforced by the database, not a
    -- read-then-act guard. A retry that races a committed order fails here.
    CREATE UNIQUE INDEX IF NOT EXISTS orders_one_entry_per_trigger
        ON orders(trigger_id, environment, purpose)
        WHERE trigger_id IS NOT NULL AND purpose = 'ENTRY';
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(order_id),
        environment TEXT NOT NULL,
        symbol TEXT NOT NULL,
        filled_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS positions ({_POSITIONS_DEFINITION});
    CREATE INDEX IF NOT EXISTS positions_state ON positions(state);
    CREATE INDEX IF NOT EXISTS positions_account_id ON positions(account_id);
    -- Slice 11 replay/pagination indexes. ``fills(order_id)`` is mandatory:
    -- it is an FK and the accept replay looks the fill up by order. The
    -- trigger indexes below are current as of schema v19 (trigger provenance).
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
    {_V27_DDL[0]}
    -- D7 hardening: the chain is append-only in the database itself; an
    -- application bug (or a casual writer) cannot rewrite or remove a row.
    DROP TRIGGER IF EXISTS audit_events_no_update;
    CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;
    DROP TRIGGER IF EXISTS audit_events_no_delete;
    CREATE TRIGGER audit_events_no_delete
        BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;
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
    {_V26_DDL[0]}
    {_V26_DDL[1]}
{_OPPORTUNITY_CONDITIONS_DDL}
{_V22_DDL}
{_V23_DDL}
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


def _migration_recorded(connection: sqlite3.Connection, version: int) -> bool:
    """Whether one specific migration version row exists.

    ``MAX(version)`` alone is insufficient: a repair test (or an operator
    recovery) can remove one version while newer rows remain. Each idempotent
    backfill is re-run when its own version row is absent.
    """
    if not _table_exists(connection, "schema_migrations"):
        return False
    row = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    return row is not None


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
_TRIGGER_EVENT_ADDITIONS = (
    ("snapshot_hash", "TEXT NOT NULL DEFAULT ''"),
    ("as_of", "TEXT NOT NULL DEFAULT ''"),
    ("data_quality", "TEXT NOT NULL DEFAULT ''"),
    ("condition_count", "INTEGER NOT NULL DEFAULT 0"),
)
_ORDER_ADDITIONS = (
    ("trigger_id", "TEXT"),
    ("decision_id", "TEXT"),
    ("purpose", "TEXT NOT NULL DEFAULT 'ENTRY'"),
)
_RISK_VERDICT_ADDITIONS = (
    ("spread_bps", "REAL"),
    ("expected_slippage_bps", "REAL"),
    ("daily_loss_usd", "TEXT"),
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
    """Seed the built-in ANALYSIS/ENTRY_RECHECK/MANAGEMENT/REVIEW templates.

    ``INSERT OR IGNORE`` keeps the seed idempotent; an operator's edits and
    activations are never overwritten by a later initialize. ``MANAGEMENT`` was
    added in v24 and ``REVIEW`` in v25 so each LLM role has a versioned,
    pinnable template.
    """
    now = _PROMPT_SEED_CREATED_AT
    for role in (
        PromptRole.ANALYSIS,
        PromptRole.ENTRY_RECHECK,
        PromptRole.MANAGEMENT,
        PromptRole.REVIEW,
    ):
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


def _condition_rows_from_payload(
    payload: dict[str, Any], *, opportunity_id: str
) -> list[tuple[Any, ...]]:
    """Flatten one opportunity contract payload into condition rows.

    The DB row's ``opportunity_id`` is authoritative; a payload whose embedded
    id disagrees is treated as malformed so the backfill skips it.
    """
    if payload.get("opportunity_id") != opportunity_id:
        raise ValueError("payload opportunity_id does not match the row")
    rows: list[tuple[Any, ...]] = []
    for kind, key in (
        ("TRIGGER", "trigger_conditions"),
        ("INVALIDATION", "invalidation_conditions"),
    ):
        items = payload.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"{key} is not a list")
        for seq, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{seq}] is not an object")
            condition_id = item["condition_id"]
            condition_type = item["type"]
            params = item["params"]
            window = item["window"]
            required_data = item["required_data"]
            evaluable = item["evaluable"]
            if not isinstance(condition_id, str) or not condition_id:
                raise ValueError("condition_id is missing")
            if not isinstance(condition_type, str) or not condition_type:
                raise ValueError("condition type is missing")
            if not isinstance(params, dict) or not isinstance(window, dict):
                raise ValueError("params/window must be objects")
            if not isinstance(required_data, list):
                raise ValueError("required_data must be a list")
            if not isinstance(evaluable, bool):
                raise ValueError("evaluable must be a boolean")
            window_start = window["start"]
            window_end = window["end"]
            if not isinstance(window_start, str) or not isinstance(window_end, str):
                raise ValueError("window bounds must be timestamps")
            last_result = item.get("last_result")
            try:
                normalized_start = normalize_timestamp(window_start)
                normalized_end = normalize_timestamp(window_end)
            except (TypeError, ValueError) as exc:
                raise ValueError("window bounds must be timestamps") from exc
            rows.append(
                (
                    condition_id,
                    opportunity_id,
                    kind,
                    seq,
                    condition_type,
                    json.dumps(params, sort_keys=True),
                    normalized_start,
                    normalized_end,
                    json.dumps(required_data, sort_keys=True),
                    int(evaluable),
                    item.get("unavailable_reason"),
                    item.get("last_value"),
                    item.get("last_evaluated_at"),
                    None if last_result is None else int(bool(last_result)),
                )
            )
    return rows


def _backfill_opportunity_conditions(connection: sqlite3.Connection) -> None:
    """Materialise the condition mirror from every opportunity payload.

    Callers own the transaction: SQL/programming errors roll it back and
    surface, while only a malformed payload is skipped with a warning.
    """
    rows = connection.execute("SELECT opportunity_id, payload_json FROM opportunities").fetchall()
    for row in rows:
        opportunity_id = str(row[0])
        try:
            payload = json.loads(str(row[1]))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            condition_rows = _condition_rows_from_payload(payload, opportunity_id=opportunity_id)
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            ConditionParamsError,
        ) as exc:
            # Only a malformed payload is skipped. SQL and programming
            # failures below surface and roll the migration back.
            _LOGGER.warning(
                "opportunity_condition_backfill_skipped",
                extra={"opportunity_id": opportunity_id, "error": str(exc)},
            )
            continue
        for condition_row in condition_rows:
            # Re-running the backfill (an operator repair deletes the v18
            # version row) must not wipe evaluation state recorded since the
            # row was created. Update only the static contract columns and
            # keep ``last_value/last_evaluated_at/last_result`` untouched.
            connection.execute(
                """
                INSERT INTO opportunity_conditions (
                    condition_id, opportunity_id, kind, seq, type,
                    params_json, window_start, window_end, required_data_json,
                    evaluable, unavailable_reason, last_value,
                    last_evaluated_at, last_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id, condition_id) DO UPDATE SET
                    kind = excluded.kind,
                    seq = excluded.seq,
                    type = excluded.type,
                    params_json = excluded.params_json,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    required_data_json = excluded.required_data_json,
                    evaluable = excluded.evaluable,
                    unavailable_reason = excluded.unavailable_reason
                """,
                condition_row,
            )


def _migrate_v18(connection: sqlite3.Connection) -> None:
    """Create the condition mirror table and backfill it from payload rows."""
    if not _table_exists(connection, "opportunities"):
        return
    connection.executescript(_OPPORTUNITY_CONDITIONS_DDL)
    with connection:
        _backfill_opportunity_conditions(connection)


def _migrate_v19(connection: sqlite3.Connection) -> None:
    """Add trigger snapshot provenance columns and backfill from payloads (v19).

    The four columns make the trigger row queryable without parsing JSON. A
    malformed payload is skipped with a log and keeps the column defaults; it
    never aborts boot. Add-columns and the backfill run under one
    ``BEGIN IMMEDIATE`` transaction, so two concurrent ``initialize_database``
    calls during a v18 -> v19 upgrade cannot both observe the column missing
    and raise ``duplicate column name``: the loser waits for the write lock and
    then re-reads ``PRAGMA table_info`` inside its own transaction.
    """
    if not _table_exists(connection, "trigger_events"):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(connection, "trigger_events", _TRIGGER_EVENT_ADDITIONS)
        rows = connection.execute(
            "SELECT trigger_id, payload_json FROM trigger_events "
            "WHERE snapshot_hash = '' OR as_of = ''"
        ).fetchall()
        for row in rows:
            trigger_id = str(row[0])
            try:
                payload = json.loads(str(row[1]))
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                snapshot = payload.get("snapshot")
                if not isinstance(snapshot, dict):
                    raise ValueError("snapshot is not an object")
                results = payload.get("condition_results")
                condition_count = len(results) if isinstance(results, list) else 0
                snapshot_hash = snapshot.get("snapshot_hash")
                as_of = snapshot.get("as_of")
                data_quality = snapshot.get("data_quality")
                connection.execute(
                    """
                    UPDATE trigger_events
                    SET snapshot_hash = ?, as_of = ?, data_quality = ?,
                        condition_count = ?
                    WHERE trigger_id = ?
                    """,
                    (
                        snapshot_hash if isinstance(snapshot_hash, str) else "",
                        as_of if isinstance(as_of, str) else "",
                        data_quality if isinstance(data_quality, str) else "",
                        condition_count,
                        trigger_id,
                    ),
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                _LOGGER.warning(
                    "trigger_event_backfill_skipped",
                    extra={"trigger_id": trigger_id, "error": str(exc)},
                )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_v20(connection: sqlite3.Connection) -> None:
    """Add the paper-OMS trigger binding and measured verdict inputs (v20).

    ``orders`` gains ``trigger_id``/``decision_id``/``purpose`` plus the partial
    UNIQUE index that makes "at most one ENTRY order per trigger" a database
    invariant; ``risk_verdicts`` gains the measured inputs the gateway needs to
    re-derive ``inputs_hash`` without re-measuring.
    """
    if not _table_exists(connection, "orders") and not _table_exists(connection, "risk_verdicts"):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(connection, "orders", _ORDER_ADDITIONS)
        _add_missing_columns(connection, "risk_verdicts", _RISK_VERDICT_ADDITIONS)
        if _table_exists(connection, "orders"):
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS orders_one_entry_per_trigger "
                "ON orders(trigger_id, environment, purpose) "
                "WHERE trigger_id IS NOT NULL AND purpose = 'ENTRY'"
            )
        if _table_exists(connection, "risk_verdicts"):
            connection.execute(
                "CREATE INDEX IF NOT EXISTS risk_verdicts_trigger_id ON risk_verdicts(trigger_id)"
            )
        if _table_exists(connection, "positions"):
            connection.execute(
                "CREATE INDEX IF NOT EXISTS positions_account_id ON positions(account_id)"
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


# The foreign keys v21 introduces, as ``(child table, child column, parent)``.
# Only these are *new* constraints: a legacy orphan on an older foreign key
# violates nothing v21 added, so it is carried rather than aborting boot.
_V21_NEW_FOREIGN_KEYS: tuple[tuple[str, str, str], ...] = (
    ("risk_verdicts", "decision_id", "decisions"),
    ("orders", "risk_verdict_id", "risk_verdicts"),
    ("positions", "account_id", "paper_accounts"),
)


def _v21_foreign_keys_present(connection: sqlite3.Connection) -> bool:
    """Whether every existing rebuilt table already carries its v21 foreign keys.

    The rebuild is one transaction, so testing the new child foreign keys is
    enough to tell whether a concurrent initializer has already applied it.
    """
    expected = {child: (column, parent) for child, column, parent in _V21_NEW_FOREIGN_KEYS}
    seen = False
    for table, (column, parent) in expected.items():
        if not _table_exists(connection, table):
            continue
        seen = True
        targets = {
            (row[3], row[2]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        if (column, parent) not in targets:
            return False
    return seen


class _V21MigrationAbort(sqlite3.IntegrityError):
    """A v21 abort whose message is already complete and must not be re-wrapped."""


def _v21_orphan_foreign_key(
    connection: sqlite3.Connection,
) -> tuple[str, str] | None:
    """The first orphaned child row on a foreign key v21 introduced.

    ``PRAGMA foreign_key_check`` reports one row per child row whose parent is
    missing. ``PRAGMA foreign_keys=OFF`` disables *enforcement* for the copy,
    not this scan. Only the ``(child, parent)`` pairs in
    ``_V21_NEW_FOREIGN_KEYS`` are new constraints; sorting keeps the reported
    violation deterministic across SQLite versions.
    """
    new_pairs = {(child, parent) for child, _column, parent in _V21_NEW_FOREIGN_KEYS}
    violations = sorted(
        {
            (str(row[0]), str(row[2]))
            for row in connection.execute("PRAGMA foreign_key_check")
            if (str(row[0]), str(row[2])) in new_pairs
        }
    )
    return violations[0] if violations else None


def _migrate_v21(connection: sqlite3.Connection) -> None:
    """Add the residual foreign keys and the enum CHECKs (v21).

    SQLite cannot add a foreign key or a CHECK with ``ALTER TABLE``, so each of
    the six affected tables is rebuilt with the v21 definition: rename, create,
    copy every existing column, drop the renamed table. Every index is dropped
    with the old table and recreated by ``_SCHEMA_SQL`` afterwards.

    ``PRAGMA foreign_keys=OFF`` defers *foreign-key* enforcement for the copy
    only; it does not disable CHECK constraints. After the copy, still inside
    the transaction, ``PRAGMA foreign_key_check`` looks for child rows whose
    parent is missing. A legacy row that violates a *new* constraint -- a CHECK
    or one of the foreign keys v21 introduces -- aborts the migration: the
    transaction is rolled back, the database stays at v20, no data is changed,
    and the raised ``sqlite3.IntegrityError`` names the offending table (and,
    for a foreign key, the missing parent) so the operator can repair the row.
    Rows are never defaulted, rewritten, or dropped. ``legacy_alter_table`` is
    enabled so the rename does not rewrite the REFERENCES clauses of the
    *other* tables (``fills`` references ``orders``, ``budget_ledger``
    references ``llm_calls``, ``risk_verdicts`` references ``trigger_events``);
    without it those clauses would follow the renamed table and dangle once it
    is dropped.
    """
    if not any(_table_exists(connection, table) for table, _definition, _columns in _V21_REBUILDS):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA legacy_alter_table=ON")
    table = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Re-check under the write lock: a concurrent initializer may have
        # already performed this rebuild after the unlocked version check that
        # led here, in which case rebuilding again would be wasted work.
        if _v21_foreign_keys_present(connection):
            connection.execute("COMMIT")
            return
        for table, definition, columns in _V21_REBUILDS:
            if not _table_exists(connection, table):
                continue
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            copied = [name for name in columns if name in existing]
            if not copied:
                continue
            old_table = f"{table}_v20"
            connection.execute(f"DROP TABLE IF EXISTS {old_table}")
            connection.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
            connection.execute(f"CREATE TABLE {table} ({definition})")
            column_list = ", ".join(copied)
            connection.execute(
                f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {old_table}"
            )
            connection.execute(f"DROP TABLE {old_table}")
        # The copy declares the v21 foreign keys while enforcement is off, so
        # check them explicitly before the schema is stamped v21. A v20 row
        # whose parent is missing must abort exactly like a CHECK violation.
        orphan = _v21_orphan_foreign_key(connection)
        if orphan is not None:
            child, parent = orphan
            raise _V21MigrationAbort(
                f"v21 migration aborted: a legacy row in table {child!r} references a "
                f"missing row in table {parent!r} (PRAGMA foreign_key_check). The database "
                "was left at schema v20 with no data changed. Repair or remove the offending "
                "row, then restart."
            )
        connection.execute("COMMIT")
    except _V21MigrationAbort:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise sqlite3.IntegrityError(
            f"v21 migration aborted: a legacy row in table {table!r} violates a new "
            f"constraint ({exc}). The database was left at schema v20 with no data "
            "changed. Repair or remove the offending row, then restart."
        ) from exc
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute("PRAGMA foreign_keys=ON")


def _v22_objects_present(connection: sqlite3.Connection) -> bool:
    """Whether every v22 table, index, and trigger already exists.

    The migration is one transaction, so testing the full set tells a
    concurrent initializer that the other writer already applied it.
    """
    if not (
        _table_exists(connection, "trade_logs") and _table_exists(connection, "position_events")
    ):
        return False
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(trade_logs)")}
    if not set(_TRADE_LOGS_INDEX_NAMES) <= indexes:
        return False
    triggers = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    return set(_POSITION_EVENTS_TRIGGER_NAMES) <= triggers


def _backfill_position_source_trigger_ids(connection: sqlite3.Connection) -> None:
    """Copy ``source_trigger_id`` from each position's ENTRY order into its payload.

    Before this slice a position payload did not carry ``source_trigger_id``,
    so the exit verdict had no real parent trigger and close failed as
    ``DATA_UNAVAILABLE``. The order row already knows the trigger; the backfill
    matches an ENTRY order by the ``position_id`` recorded in its payload and
    writes the trigger id into the OPEN position's payload. No column is added;
    a malformed row is skipped with a warning rather than aborting the upgrade.
    """
    if not (_table_exists(connection, "positions") and _table_exists(connection, "orders")):
        return
    position_columns = {row[1] for row in connection.execute("PRAGMA table_info(positions)")}
    order_columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
    if not {"position_id", "state", "payload_json"} <= position_columns:
        return
    if not {"trigger_id", "purpose", "payload_json"} <= order_columns:
        return
    triggers_by_position: dict[str, str] = {}
    for row in connection.execute(
        "SELECT trigger_id, payload_json FROM orders "
        "WHERE purpose = 'ENTRY' AND trigger_id IS NOT NULL"
    ):
        try:
            order_payload = json.loads(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            _LOGGER.warning("skipping unreadable ENTRY order payload during v22 backfill")
            continue
        if not isinstance(order_payload, dict):
            continue
        position_id = order_payload.get("position_id")
        if position_id is not None:
            triggers_by_position.setdefault(str(position_id), str(row[0]))
    if not triggers_by_position:
        return
    for row in connection.execute(
        "SELECT position_id, payload_json FROM positions WHERE state = 'OPEN'"
    ).fetchall():
        try:
            payload = json.loads(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            _LOGGER.warning(
                "skipping unreadable position payload during v22 backfill: %s",
                row[0],
            )
            continue
        if not isinstance(payload, dict) or payload.get("source_trigger_id"):
            continue
        position_id = str(payload.get("position_id") or row[0])
        trigger_id = triggers_by_position.get(position_id)
        if trigger_id is None:
            _LOGGER.warning(
                "no ENTRY order to backfill source_trigger_id for position %s", position_id
            )
            continue
        payload["source_trigger_id"] = trigger_id
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), row[0]),
        )


def _migrate_v22(connection: sqlite3.Connection) -> None:
    """Add ``trade_logs`` and the append-only ``position_events`` timeline (v22).

    Both tables are new; the only data change is the ``source_trigger_id``
    backfill for pre-v22 OPEN positions, which copies the trigger id already
    recorded on the matching ENTRY order. ``IF NOT EXISTS`` plus the trigger
    drop/create makes the migration idempotent; ``BEGIN IMMEDIATE`` serializes
    two concurrent initializers and keeps the backfill in the same transaction.
    A fresh database has no ``positions`` table yet, so the migration is a
    no-op and ``_SCHEMA_SQL`` creates the full schema afterwards.
    """
    if not (_table_exists(connection, "positions") and _table_exists(connection, "paper_accounts")):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: a concurrent initializer may have
        # already applied the migration after the unlocked version check that
        # led here, in which case dropping/recreating is wasted work.
        if _v22_objects_present(connection):
            connection.execute("COMMIT")
            return
        connection.execute(_TRADE_LOGS_TABLE)
        for statement in _TRADE_LOGS_INDEXES:
            connection.execute(statement)
        connection.execute(_POSITION_EVENTS_TABLE)
        for statement in _POSITION_EVENTS_TRIGGERS:
            connection.execute(statement)
        _backfill_position_source_trigger_ids(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _funding_unique_constraint_present(connection: sqlite3.Connection) -> bool:
    """Whether ``funding_accruals`` carries the ``(position_id, settlement_time)`` key."""
    for row in connection.execute("PRAGMA index_list(funding_accruals)"):
        if int(row[2]) != 1:
            continue
        columns = [str(info[2]) for info in connection.execute(f"PRAGMA index_info('{row[1]}')")]
        if columns == ["position_id", "settlement_time"]:
            return True
    return False


def _funding_has_side_size(connection: sqlite3.Connection) -> bool:
    """Whether ``funding_accruals`` records the side and size the amount was priced on.

    Callers only invoke this after confirming the table exists; ``PRAGMA
    table_info`` on a missing table simply yields no columns.
    """
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(funding_accruals)")}
    return {"side", "size"} <= columns


def _rebuild_funding_accruals_for_v23(connection: sqlite3.Connection) -> None:
    """Rebuild a pre-release v23 funding table to add ``side``/``size``.

    v23 was never released, so a table without these columns can only come from
    an intermediate build. Existing rows (if any) are preserved verbatim; the
    two new columns are empty strings because their values cannot be
    reconstructed from the old row.
    """
    connection.execute("ALTER TABLE funding_accruals RENAME TO funding_accruals_v23_old")
    connection.execute(_FUNDING_ACCRUALS_TABLE)
    connection.execute(
        """
        INSERT INTO funding_accruals (
            accrual_id, position_id, settlement_time, rate, mark_price,
            amount_usd, created_at, side, size
        )
        SELECT accrual_id, position_id, settlement_time, rate, mark_price,
               amount_usd, created_at, '', ''
        FROM funding_accruals_v23_old
        """
    )
    connection.execute("DROP TABLE funding_accruals_v23_old")


def _ensure_funding_accruals_shape(connection: sqlite3.Connection) -> None:
    """Repair a v23 funding table recorded before ``side``/``size`` landed.

    The migration is unmerged, so a database stamped v23 may predate the
    columns. Re-running initialization repairs it idempotently.
    """
    if not _table_exists(connection, "funding_accruals"):
        return
    if _funding_has_side_size(connection):
        return
    # ``commit()`` is a no-op without an open transaction, so the repair never
    # tries to start ``BEGIN IMMEDIATE`` inside one.
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _rebuild_funding_accruals_for_v23(connection)
        connection.execute("COMMIT")
    except Exception:
        connection.rollback()
        raise


def _reports_has_symbol_column(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "reports"):
        return False
    return any(str(row[1]) == "symbol" for row in connection.execute("PRAGMA table_info(reports)"))


def _ensure_reports_symbol_index(connection: sqlite3.Connection) -> None:
    """Create the funding-history covering index when the table can carry it.

    Very old synthetic schemas have a minimal ``reports`` table without the
    ``symbol`` column; the index is skipped there and the normal upgraded
    schema (v9+ migrations add the column) receives it.
    """
    if _reports_has_symbol_column(connection):
        connection.execute(_REPORTS_SYMBOL_GENERATED_INDEX)


def _v23_objects_present(connection: sqlite3.Connection) -> bool:
    """Whether the v23 funding table, report index, and event actions exist."""
    if not _table_exists(connection, "funding_accruals"):
        return False
    if not _funding_has_side_size(connection):
        return False
    if not _funding_unique_constraint_present(connection):
        return False
    if _reports_has_symbol_column(connection):
        index_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'reports_symbol_generated'"
        ).fetchone()
        if index_row is None:
            return False
    if not _table_exists(connection, "position_events"):
        return True
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'position_events'"
    ).fetchone()
    if row is None or row[0] is None:
        return True
    definition = str(row[0])
    return all(f"'{action}'" in definition for action in ("PARTIAL_CLOSE", "ADD", "FUNDING"))


def _rebuild_position_events_for_v23(connection: sqlite3.Connection) -> None:
    """Widen the append-only event action CHECK, preserving every row.

    SQLite cannot widen a CHECK with ``ALTER TABLE``. The append-only triggers
    are dropped first, the table is rebuilt with the new definition, its rows
    are copied verbatim, and the triggers are recreated.
    """
    for statement in _POSITION_EVENTS_TRIGGERS[:1] + _POSITION_EVENTS_TRIGGERS[2:3]:
        connection.execute(statement)
    connection.execute("DROP TABLE IF EXISTS position_events_v22")
    connection.execute("ALTER TABLE position_events RENAME TO position_events_v22")
    connection.execute(_POSITION_EVENTS_TABLE)
    connection.execute(
        """
        INSERT INTO position_events (position_id, seq, at, action, order_id, note)
        SELECT position_id, seq, at, action, order_id, note FROM position_events_v22
        """
    )
    connection.execute("DROP TABLE position_events_v22")
    for statement in _POSITION_EVENTS_TRIGGERS[1::2]:
        connection.execute(statement)


def _migrate_v23(connection: sqlite3.Connection) -> None:
    """Add ``funding_accruals`` and widen the position-event actions (v23).

    The funding table carries the ``(position_id, settlement_time)`` idempotency
    constraint. ``position_events`` is rebuilt because SQLite cannot widen its
    CHECK with ``ALTER TABLE``; the append-only triggers are dropped, the rows
    copied verbatim, and the triggers recreated. ``BEGIN IMMEDIATE`` plus the
    in-lock re-check matches the v22 pattern, so two concurrent initializers
    cannot both rebuild.
    """
    if not _table_exists(connection, "positions"):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _v23_objects_present(connection):
            connection.execute("COMMIT")
            return
        connection.execute(_FUNDING_ACCRUALS_TABLE)
        if not _funding_has_side_size(connection):
            _rebuild_funding_accruals_for_v23(connection)
        _ensure_reports_symbol_index(connection)
        if _table_exists(connection, "position_events"):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'position_events'"
            ).fetchone()
            definition = "" if row is None or row[0] is None else str(row[0])
            if not all(
                f"'{action}'" in definition for action in ("PARTIAL_CLOSE", "ADD", "FUNDING")
            ):
                _rebuild_position_events_for_v23(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _migrate_v24(connection: sqlite3.Connection) -> None:
    """Seed the MANAGEMENT prompt family and stamp schema v24 (slice 25).

    The management decision path renders ``management``/``management:system``
    through the same immutable prompt lifecycle as the analysis and entry roles,
    so both templates are seeded as active v1 idempotently. An operator's edits
    and activations are never overwritten (``INSERT OR IGNORE``). No table is
    added by this migration; the position ledger already has the indexes the
    management reads use.
    """
    if not (
        _table_exists(connection, "prompt_templates")
        and _table_exists(connection, "prompt_versions")
    ):
        # A fresh database has no prompt tables yet; ``_SCHEMA_SQL`` creates
        # them and ``_seed_prompt_defaults`` seeds them later in
        # ``initialize_database``.
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _seed_prompt_defaults(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _migrate_v25(connection: sqlite3.Connection) -> None:
    """Seed the REVIEW prompt family and link reviews from trade logs (slice 28).

    ``REVIEW`` joins the seeded roles so the advisory review path renders
    through the same immutable prompt lifecycle as every other role. The
    ``trade_logs.review_decision_id`` column is additive: a populated v24
    database keeps every row and payload byte-identical, and the column is
    indexed so a persisted review can be found from either side. Both steps are
    idempotent; a fresh database has no tables yet, so ``_SCHEMA_SQL`` creates
    the v25 shape and seeds the prompts afterwards.
    """
    if not (
        _table_exists(connection, "prompt_templates") or _table_exists(connection, "trade_logs")
    ):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(connection, "trade_logs"):
            columns = {row[1] for row in connection.execute("PRAGMA table_info(trade_logs)")}
            if "review_decision_id" not in columns:
                connection.execute(
                    "ALTER TABLE trade_logs ADD COLUMN "
                    "review_decision_id TEXT REFERENCES decisions(decision_id)"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS trade_logs_review_decision "
                "ON trade_logs(review_decision_id)"
            )
        if _table_exists(connection, "prompt_templates") and _table_exists(
            connection, "prompt_versions"
        ):
            _seed_prompt_defaults(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _migrate_v26(connection: sqlite3.Connection) -> None:
    """Add the ``config`` table for persisted runtime settings (slice 32).

    The table is additive and idempotent: a populated v25 database keeps every
    existing row and gains an empty ``config`` table plus its ``updated_at``
    index. A fresh database runs this before ``_SCHEMA_SQL``, which creates the
    same shape with ``IF NOT EXISTS``.
    """
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _V26_DDL:
            connection.execute(statement)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _migrate_v27(connection: sqlite3.Connection) -> None:
    """Add the ``(event_type, seq DESC)`` audit lookup index (slice 34).

    Additive and idempotent: a populated v26 database keeps every audit row and
    gains the index used to resolve the newest event of one type in O(log n).
    A database without the append-only ``audit_events`` table is left untouched;
    the fresh schema creates the same index next to the table.
    """
    if not _table_exists(connection, "audit_events"):
        return
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _V27_DDL:
            connection.execute(statement)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _opportunity_conditions_pk(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    rows = connection.execute("PRAGMA table_info(opportunity_conditions)").fetchall()
    keyed = sorted((row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5]))
    return tuple(str(row[1]) for row in keyed)


def _rebuild_opportunity_conditions(connection: sqlite3.Connection) -> None:
    """Rebuild a v18 table whose primary key predates the composite fix.

    Any transaction left open by a prior repair is committed first: SQLite
    cannot start a transaction within a transaction, and the rebuild must stay
    one atomic unit (DDL included).
    """
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: a concurrent initializer may have
        # already performed this rebuild after the unlocked check that led
        # here, in which case rebuilding again would be wasted work.
        if _opportunity_conditions_pk(connection) == ("opportunity_id", "condition_id"):
            connection.execute("COMMIT")
            return
        for index_name in _OPPORTUNITY_CONDITION_INDEX_NAMES:
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        connection.execute(
            "ALTER TABLE opportunity_conditions RENAME TO opportunity_conditions_v18_old"
        )
        connection.execute(_OPPORTUNITY_CONDITIONS_TABLE)
        for statement in _OPPORTUNITY_CONDITIONS_INDEXES[: len(_OPPORTUNITY_CONDITION_INDEX_NAMES)]:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO opportunity_conditions (
                condition_id, opportunity_id, kind, seq, type, params_json,
                window_start, window_end, required_data_json, evaluable,
                unavailable_reason, last_value, last_evaluated_at, last_result
            )
            SELECT
                condition_id, opportunity_id, kind, seq, type, params_json,
                window_start, window_end, required_data_json, evaluable,
                unavailable_reason, last_value, last_evaluated_at, last_result
            FROM opportunity_conditions_v18_old
            """
        )
        # The pre-fix global PK may already have destroyed mirror rows (two
        # opportunities holding the same condition id). Re-materialise every
        # opportunity from its payload inside the same transaction.
        _backfill_opportunity_conditions(connection)
        connection.execute("DROP TABLE opportunity_conditions_v18_old")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _ensure_opportunity_conditions_shape(connection: sqlite3.Connection) -> None:
    """Repair a v18 database stamped before the composite primary key landed."""
    if not _table_exists(connection, "opportunity_conditions"):
        return
    if _opportunity_conditions_pk(connection) == ("opportunity_id", "condition_id"):
        return
    _rebuild_opportunity_conditions(connection)


def _ensure_opportunities_expires_at(connection: sqlite3.Connection) -> None:
    """Add and normalise the queryable expiry column used by the due selector.

    Values are canonical UTC with fixed millisecond precision so the TEXT
    range comparison in ``list_due_opportunities`` matches every instant
    regardless of the offset/precision the payload was written with. A prior
    boot may have added the column but crashed before backfilling it, so rows
    with a NULL column are rebuilt from ``payload_json`` too.
    """
    if not _table_exists(connection, "opportunities"):
        return
    if connection.in_transaction:
        connection.commit()
    # Acquire the write lock before re-reading the table shape: a concurrent
    # initializer may add ``expires_at`` between an unlocked check and the
    # ALTER, which would otherwise raise "duplicate column name". The context
    # manager commits (or rolls back) the ALTER and the backfill together.
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunities)")}
        if "expires_at" not in columns:
            connection.execute("ALTER TABLE opportunities ADD COLUMN expires_at TEXT")
        rows = connection.execute(
            "SELECT opportunity_id, payload_json, expires_at FROM opportunities"
        ).fetchall()
        for opportunity_id, payload_json, current in rows:
            value = current
            if value is None:
                try:
                    payload = json.loads(str(payload_json))
                except (json.JSONDecodeError, TypeError, ValueError):
                    _LOGGER.warning(
                        "opportunity_expires_at_backfill_skipped",
                        extra={
                            "opportunity_id": opportunity_id,
                            "reason": "payload_json is not valid JSON",
                        },
                    )
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning(
                        "opportunity_expires_at_backfill_skipped",
                        extra={
                            "opportunity_id": opportunity_id,
                            "reason": "payload_json is not an object",
                        },
                    )
                    continue
                raw = payload.get("expires_at")
                if not isinstance(raw, str):
                    _LOGGER.warning(
                        "opportunity_expires_at_backfill_skipped",
                        extra={
                            "opportunity_id": opportunity_id,
                            "reason": "payload has no string expires_at",
                        },
                    )
                    continue
                value = raw
            try:
                normalized = normalize_timestamp(value)
            except (TypeError, ValueError):
                # A row left NULL here is never selected by
                # ``list_due_opportunities``, so the skip must be diagnosable.
                _LOGGER.warning(
                    "opportunity_expires_at_backfill_skipped",
                    extra={
                        "opportunity_id": opportunity_id,
                        "reason": "expires_at is not a valid timestamp",
                    },
                )
                continue
            if normalized != current:
                connection.execute(
                    "UPDATE opportunities SET expires_at = ? WHERE opportunity_id = ?",
                    (normalized, opportunity_id),
                )


def _enable_wal(connection: sqlite3.Connection) -> None:
    """Switch the connection's database to WAL, tolerating a concurrent boot.

    ``PRAGMA journal_mode=WAL`` takes an exclusive lock and, unlike ordinary
    writes, can return ``SQLITE_BUSY`` without invoking the busy handler even
    after ``busy_timeout`` is set. A short bounded retry lets the other
    initializer finish its switch instead of failing with ``database is
    locked``. Any other ``OperationalError`` propagates unchanged.
    """
    for attempt in range(1, _WAL_SWITCH_ATTEMPTS + 1):
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt == _WAL_SWITCH_ATTEMPTS:
                raise
            time.sleep(_WAL_SWITCH_BACKOFF_SECONDS * attempt)


def initialize_database(settings: Settings) -> None:
    """Create the data directory and the schema, currently v27.

    v19 adds the trigger snapshot provenance columns (``snapshot_hash``,
    ``as_of``, ``data_quality``, ``condition_count``) and backfills them from
    recorded payloads. v20 binds paper orders to their trigger (with a partial
    UNIQUE index) and stores the measured risk inputs on each verdict. v21 adds
    the residual foreign keys and the enum CHECK constraints, rebuilding the six
    affected tables. v22 adds ``trade_logs`` (unique ``position_id``) and the
    append-only ``position_events`` timeline. v23 adds ``funding_accruals``
    (unique ``position_id + settlement_time``, recording the side and size the
    amount was priced on) and widens the event action CHECK to
    ``PARTIAL_CLOSE``/``ADD``/``FUNDING``. v24 seeds the ``MANAGEMENT`` prompt
    family (idempotent) for the position-management path. v25 seeds the
    ``REVIEW`` prompt family and adds the nullable
    ``trade_logs.review_decision_id`` link (indexed), preserving every existing
    trade-log row and payload. v26 adds the additive ``config`` key/value table
    (``key`` primary key plus an ``updated_at`` index) so runtime settings can
    be persisted and audited in one transaction. v27 adds the additive
    ``audit_events(event_type, seq DESC)`` index that makes the newest-event-of-
    one-type lookup a bounded index seek instead of a growing reverse scan.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    db_path = database_path(settings)
    connection = sqlite3.connect(db_path)
    try:
        # Install the busy handler *before* any lock-taking pragma. The WAL
        # switch needs an exclusive lock; configuring it first let a
        # concurrent initializer fail with SQLITE_BUSY before the handler
        # existed. ``_enable_wal`` additionally retries the switch, because
        # that pragma does not always consult the handler.
        configure_connection(connection)
        connection.execute("PRAGMA synchronous=NORMAL")
        _enable_wal(connection)
        _ensure_opportunities_expires_at(connection)
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
        if _schema_version(connection) < 18 or not _migration_recorded(connection, 18):
            _migrate_v18(connection)
        if _schema_version(connection) < 19 or not _migration_recorded(connection, 19):
            _migrate_v19(connection)
        if _schema_version(connection) < 20 or not _migration_recorded(connection, 20):
            _migrate_v20(connection)
        if _schema_version(connection) < 21 or not _migration_recorded(connection, 21):
            _migrate_v21(connection)
        if _schema_version(connection) < 22 or not _migration_recorded(connection, 22):
            _migrate_v22(connection)
        if _schema_version(connection) < 23 or not _migration_recorded(connection, 23):
            _migrate_v23(connection)
        if _schema_version(connection) < 24 or not _migration_recorded(connection, 24):
            _migrate_v24(connection)
        if _schema_version(connection) < 25 or not _migration_recorded(connection, 25):
            _migrate_v25(connection)
        if _schema_version(connection) < 26 or not _migration_recorded(connection, 26):
            _migrate_v26(connection)
        if _schema_version(connection) < 27 or not _migration_recorded(connection, 27):
            _migrate_v27(connection)
        _ensure_opportunity_conditions_shape(connection)
        _ensure_funding_accruals_shape(connection)
        with connection:
            # ``_SCHEMA_SQL`` drops and recreates the immutability trigger, a
            # check-then-DDL pair. Run the whole batch inside one write
            # transaction so concurrent initializers cannot interleave the
            # DROP and the CREATE and observe "trigger ... already exists"
            # (or race any other DDL). ``executescript`` commits any pending
            # transaction first, hence BEGIN/COMMIT live inside the script.
            connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}\nCOMMIT;")
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
                (17, "2026-11-09T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (18, "2026-11-09T06:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (19, "2026-11-09T12:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (20, "2026-11-10T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (21, "2026-11-11T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (22, "2026-11-13T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (23, "2026-11-14T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (24, "2026-11-14T12:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (25, "2026-11-14T18:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (26, "2026-11-15T00:00:00.000Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (27, "2026-11-15T12:00:00.000Z"),
            )
            _seed_prompt_defaults(connection)
            _ensure_reports_symbol_index(connection)
    finally:
        connection.close()
        # The DB may hold a trading journal; never leave it group/world readable.
        os.chmod(db_path, 0o600)
