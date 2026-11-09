"""Opportunity ledger integration: persistence, display evaluation, API, CLI."""

from __future__ import annotations

import inspect
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.sqlite_store import database_path
from oft.api.app import create_app
from oft.application.opportunity_service import (
    OpportunityNotFound,
    OpportunityService,
    available_data_from_snapshot,
)
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.llm_output import OpportunityDraft
from oft.domain.opportunities import ConditionDraftError, OpportunityTransitionError
from oft.ports.store import (
    ConditionRecord,
    OpportunityEventRecord,
    OpportunityRecord,
    ReportRecord,
    ScanRecord,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_ISO = "2026-11-06T12:00:00.000Z"
# Derived from the fixed clock so the fixture never becomes a time bomb.
FUTURE = (NOW + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
PAST = "2026-01-01T00:00:00.000Z"


def _settings(tmp_path: Path, *, port: int = 8765) -> Settings:
    return Settings(env="test", port=port, data_dir=tmp_path / "data")


def _params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "price": None,
        "price_low": None,
        "price_high": None,
        "threshold": None,
        "unit": None,
        "comparator": None,
        "lookback_bars": None,
        "session_states": [],
        "note": None,
    }
    base.update(overrides)
    return base


def _trigger(
    condition_type: str,
    *,
    draft_id: str,
    **params: Any,
) -> dict[str, Any]:
    return {
        "draft_id": draft_id,
        "type": condition_type,
        "params": _params(**params),
        "window": {"start": NOW_ISO, "end": FUTURE},
        "required_data": [],
        "description": f"{condition_type} trigger",
    }


def _invalidation(
    condition_type: str,
    *,
    price: str | None = None,
    threshold: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    return {
        "type": condition_type,
        "description": f"{condition_type} invalidation",
        "price": price,
        "threshold": threshold,
        "window_end": window_end,
    }


def _draft(
    *,
    triggers: list[dict[str, Any]],
    invalidations: list[dict[str, Any]],
    expires_at: str = FUTURE,
) -> OpportunityDraft:
    return OpportunityDraft.model_validate(
        {
            "side": "LONG",
            "zone_low": "69000.00",
            "zone_high": "71000.00",
            "entry_preference": "MARKET",
            "trigger_conditions": triggers,
            "invalidation_conditions": invalidations,
            "expires_at": expires_at,
            "risk_request": {
                "entry_price": "70000.00",
                "stop_price": "69000.00",
                "take_profits": ["72000.00"],
                "risk_pct": 0.8,
                "leverage": 2.0,
                "max_notional_usd": "1000.00",
            },
            "thesis": "range retest",
            "data_needs": ["MARK_PRICE"],
        }
    )


def _provenance() -> dict[str, Any]:
    return {
        "llm": {
            "provider": "stub",
            "model": "fixture",
            "model_snapshot": None,
            "prompt_id": "analysis",
            "prompt_version": 1,
            "prompt_hash": "a" * 64,
            "knowledge_artifact_hash": None,
            "input_hash": "b" * 64,
            "schema_version": "1.0.0",
            "temperature": 0.0,
            "seed": None,
            "request_id": "REQ-1",
            "latency_ms": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
            "cost": {
                "amount_usd": "0",
                "currency": "USD",
                "price_version": "stub/v1",
                "unpriced": True,
            },
            "recorded_at": NOW_ISO,
        },
        "calc_snapshot_id": "SNAP-1",
        "calc_snapshot_hash": "c" * 64,
        "calc_metric_version": "v1",
    }


def _data_quality() -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "reasons": [],
        "gaps": [],
        "last_event_at": NOW_ISO,
        "coverage_start": None,
    }


def _snapshot(*, last_price: str = "100.00", include_liquidations: bool = False) -> dict[str, Any]:
    return {
        "snapshot_id": "SNAP-1",
        "symbol": "BTCUSDT",
        "as_of": NOW_ISO,
        "last_price": last_price,
        "mark_price": last_price,
        "bars": [
            {"bar_start": NOW_ISO, "close": "99.00", "cvd": "9", "volume": "10", "delta": "0"},
            {"bar_start": NOW_ISO, "close": "100.00", "cvd": "10", "volume": "12", "delta": "1"},
        ],
        "contract_specs": {"tick_size": "0.01"},
        "orderflow": {
            "cvd": {"summary": {"delta": "1"}, "points": [{"cvd": "10"}]},
            "open_interest": {
                "points": [
                    {"open_interest": "10"},
                    {"open_interest": "11"},
                ]
            },
            "funding": {"current_rate": "0.0002", "history": [{"rate": "0.0002"}]},
            "liquidations": {
                "coverage": "LIVE" if include_liquidations else "NONE",
                "total_long_notional": "500",
                "total_short_notional": "600",
            },
            "footprint": {
                "bars": [
                    {
                        "cells": [
                            {"bid_volume": "3", "ask_volume": "1"},
                        ]
                    }
                ]
            },
        },
        "session": {"underlying_session": "REGULAR", "is_no_trading": False},
    }


def _scan_record() -> ScanRecord:
    return ScanRecord(
        scan_id="SCAN-1",
        symbol="BTCUSDT",
        period="4h",
        status="DONE",
        trigger_source="MANUAL",
        metrics_only=False,
        fixture_path="",
        started_at=NOW_ISO,
        finished_at=NOW_ISO,
        window_start=NOW_ISO,
        window_end=NOW_ISO,
        trade_count=0,
        buy_volume="0",
        sell_volume="0",
        delta="0",
        cvd="0",
        data_quality="COMPLETE",
        manifest_path="",
        parquet_rows=0,
        created_at=NOW_ISO,
        report_id="RPT-1",
    )


def _create(
    settings: Settings,
    *,
    draft: OpportunityDraft,
    report_id: str = "RPT-1",
    snapshot: dict[str, Any] | None = None,
    available: frozenset[str] | None = None,
) -> tuple[Any, Any]:
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    if snapshot is not None:
        if container.store.get_scan("SCAN-1") is None:
            container.store.save_scan(_scan_record())
        container.store.save_report(
            ReportRecord(
                report_id=report_id,
                scan_id="SCAN-1",
                symbol="BTCUSDT",
                timeframe="4h",
                generated_at=NOW_ISO,
                metrics_manifest_json="[]",
                llm_decision_id=None,
                narrative_ref=None,
                data_quality="COMPLETE",
                snapshot_json=json.dumps(snapshot),
                metrics_json="{}",
            )
        )
    service = OpportunityService(store=container.store, clock=container.clock)
    opportunity = service.create_from_decision(
        opportunity_draft=draft,
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-1",
        scan_id="SCAN-1",
        report_id=report_id,
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=available,
    )
    return container, opportunity


def _full_available() -> frozenset[str]:
    return available_data_from_snapshot(_snapshot())


def _multi_condition_draft() -> OpportunityDraft:
    return _draft(
        triggers=[
            _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101"),
            _trigger("DELTA_ABOVE", draft_id="c2", threshold="0.5", lookback_bars=2),
            _trigger("LIQUIDATION_NOTIONAL_ABOVE", draft_id="c3", threshold="1000"),
        ],
        invalidations=[
            _invalidation("PRICE_CROSS_BELOW", price="69000.00"),
            _invalidation("MANUAL"),
        ],
    )


def _opportunity_payload(container: Any, opportunity_id: str) -> dict[str, Any]:
    record = container.store.get_opportunity(opportunity_id)
    assert record is not None
    return json.loads(record.payload_json)


# -- persistence ------------------------------------------------------------


def test_creation_persists_conditions_with_code_ids_and_mirror_rows(
    tmp_path: Path,
) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )

    payload = _opportunity_payload(container, opportunity.opportunity_id)
    assert [item["condition_id"] for item in payload["trigger_conditions"]] == [
        "C-1",
        "C-2",
        "C-3",
    ]
    assert [item["condition_id"] for item in payload["invalidation_conditions"]] == [
        "I-1",
        "I-2",
    ]
    assert payload["trigger_conditions"][0]["params"] == {
        "price_low": "99",
        "price_high": "101",
    }
    assert payload["trigger_conditions"][1]["params"] == {
        "threshold": "0.5",
        "lookback_bars": 2,
    }
    liquidation = payload["trigger_conditions"][2]
    assert liquidation["evaluable"] is False
    assert "LIQUIDATIONS" in liquidation["unavailable_reason"]

    rows = container.store.list_conditions(opportunity.opportunity_id)
    assert len(rows) == 5
    by_id = {row.condition_id: row for row in rows}
    for condition in (
        *payload["trigger_conditions"],
        *payload["invalidation_conditions"],
    ):
        row = by_id[condition["condition_id"]]
        assert row.type == condition["type"]
        assert row.params_json == json.dumps(condition["params"], sort_keys=True)
        assert row.window_start == condition["window"]["start"]
        assert row.window_end == condition["window"]["end"]
        assert row.required_data_json == json.dumps(condition["required_data"], sort_keys=True)
        assert row.evaluable is condition["evaluable"]

    manual = by_id["I-2"]
    assert manual.type == "TIME_WINDOW"
    assert json.loads(manual.params_json) == {"original_type": "MANUAL"}
    assert manual.evaluable is False
    assert manual.unavailable_reason == "manual invalidation is code-only"


def test_condition_mirrors_are_scoped_to_their_own_opportunity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, first = _create(
        settings,
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    second = service.create_from_decision(
        opportunity_draft=_multi_condition_draft(),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-2",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    first_rows = container.store.list_conditions(first.opportunity_id)
    second_rows = container.store.list_conditions(second.opportunity_id)

    # Both opportunities use the same per-opportunity C-n / I-n identifiers.
    assert sorted(row.condition_id for row in first_rows) == [
        "C-1",
        "C-2",
        "C-3",
        "I-1",
        "I-2",
    ]
    assert sorted(row.condition_id for row in second_rows) == [
        "C-1",
        "C-2",
        "C-3",
        "I-1",
        "I-2",
    ]

    # The table mirrors each payload exactly, for both opportunities.
    for opportunity, rows in ((first, first_rows), (second, second_rows)):
        payload = _opportunity_payload(container, opportunity.opportunity_id)
        by_id = {row.condition_id: row for row in rows}
        conditions = (
            *payload["trigger_conditions"],
            *payload["invalidation_conditions"],
        )
        assert set(by_id) == {condition["condition_id"] for condition in conditions}
        for condition in conditions:
            row = by_id[condition["condition_id"]]
            assert row.opportunity_id == opportunity.opportunity_id
            assert row.params_json == json.dumps(condition["params"], sort_keys=True)
            assert row.evaluable is condition["evaluable"]

    # A per-opportunity evaluation update touches only the addressed row.
    container.store.update_condition_evaluation(
        first.opportunity_id,
        "C-1",
        last_value="100.00",
        last_evaluated_at=NOW_ISO,
        last_result=True,
    )
    first_by_id = {
        row.condition_id: row for row in container.store.list_conditions(first.opportunity_id)
    }
    second_by_id = {
        row.condition_id: row for row in container.store.list_conditions(second.opportunity_id)
    }
    assert first_by_id["C-1"].last_value == "100.00"
    assert second_by_id["C-1"].last_value is None


def test_condition_rows_can_be_filtered_and_evaluations_updated(tmp_path: Path) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger(
                    "PRICE_TOUCH_ZONE",
                    draft_id="c1",
                    price_low="99",
                    price_high="101",
                )
            ],
            invalidations=[_invalidation("PRICE_CROSS_BELOW", price="69000.00")],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    triggers = container.store.list_conditions(opportunity.opportunity_id, kind="TRIGGER")
    assert [row.condition_id for row in triggers] == ["C-1"]

    container.store.update_condition_evaluation(
        opportunity.opportunity_id,
        "C-1",
        last_value="100.00",
        last_evaluated_at=NOW_ISO,
        last_result=True,
    )

    updated = container.store.list_conditions(opportunity.opportunity_id, kind="TRIGGER")[0]
    assert updated.last_value == "100.00"
    assert updated.last_evaluated_at == NOW_ISO
    assert updated.last_result is True


def test_update_condition_evaluation_reports_a_missing_row(tmp_path: Path) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )

    persisted = container.store.update_condition_evaluation(
        opportunity.opportunity_id,
        "C-1",
        last_value="100.00",
        last_evaluated_at=NOW_ISO,
        last_result=False,
    )
    missing = container.store.update_condition_evaluation(
        opportunity.opportunity_id,
        "C-404",
        last_value="1",
        last_evaluated_at=NOW_ISO,
        last_result=True,
    )

    assert persisted is True
    assert missing is False
    row = container.store.list_conditions(opportunity.opportunity_id)[0]
    assert row.last_result is False


def test_invalid_draft_is_rejected_without_a_partial_opportunity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    draft = _draft(
        triggers=[
            _trigger("DELTA_ABOVE", draft_id="c1", threshold="1", lookback_bars=None),
        ],
        invalidations=[],
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    service = OpportunityService(store=container.store, clock=container.clock)

    with pytest.raises(ConditionDraftError) as excinfo:
        service.create_from_decision(
            opportunity_draft=draft,
            subject={"symbol": "BTCUSDT"},
            decision_id="DEC-1",
            scan_id="SCAN-1",
            report_id="RPT-1",
            confidence=0.7,
            provenance=_provenance(),
            data_quality=_data_quality(),
            at=NOW_ISO,
            available_data=set(),
        )

    assert excinfo.value.field == "lookback_bars"
    assert container.store.list_opportunities() == ()


def test_resaving_an_opportunity_does_not_duplicate_condition_rows(
    tmp_path: Path,
) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    record = container.store.get_opportunity(opportunity.opportunity_id)
    assert record is not None
    rows = container.store.list_conditions(opportunity.opportunity_id)

    container.store.save_opportunity(record, conditions=rows)
    container.store.save_opportunity(record, conditions=rows)

    assert len(container.store.list_conditions(opportunity.opportunity_id)) == 5


def test_migration_v18_backfills_valid_payloads_and_skips_malformed_ones(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    db = database_path(settings)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("OPP-BAD", "BTCUSDT", "OPEN", NOW_ISO, NOW_ISO, "{not-json"),
        )
        connection.execute("DELETE FROM opportunity_conditions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.commit()
    finally:
        connection.close()

    container.store.initialize()  # must not abort on the malformed payload

    assert container.store.list_conditions("OPP-BAD") == ()
    rows = container.store.list_conditions(opportunity.opportunity_id)
    assert len(rows) == 5
    version_connection = sqlite3.connect(db)
    try:
        versions = {
            row[0] for row in version_connection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        version_connection.close()
    assert 18 in versions


def test_migration_v18_skips_a_payload_whose_id_mismatches_the_row(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    db = database_path(settings)
    payload = _opportunity_payload(container, opportunity.opportunity_id)
    payload["opportunity_id"] = "OPP-ELSEWHERE"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("OPP-MISMATCH", "BTCUSDT", "OPEN", NOW_ISO, NOW_ISO, json.dumps(payload)),
        )
        connection.execute("DELETE FROM opportunity_conditions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.commit()
    finally:
        connection.close()

    container.store.initialize()  # must not abort on the id mismatch

    assert container.store.list_conditions("OPP-MISMATCH") == ()
    assert container.store.list_conditions("OPP-ELSEWHERE") == ()
    assert len(container.store.list_conditions(opportunity.opportunity_id)) == 5


def test_v18_mirror_and_query_indexes_exist(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    build_container(settings, initialize=True, clock=FixedClock(NOW))
    connection = sqlite3.connect(database_path(settings))
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    finally:
        connection.close()

    assert {
        "opportunity_conditions_opportunity",
        "opportunity_conditions_type",
        "opportunity_conditions_evaluable",
        "opportunity_conditions_condition_id",
        "opportunities_state_updated",
        "opportunities_symbol_state",
        "opportunities_expires_at",
        "opportunity_events_opportunity_at",
    } <= names


def test_v18_database_with_the_old_primary_key_is_rebuilt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    build_container(settings, initialize=True, clock=FixedClock(NOW))
    db = database_path(settings)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for opportunity_id in ("OPP-OLD-A", "OPP-OLD-B"):
            connection.execute(
                "INSERT INTO opportunities "
                "(opportunity_id, symbol, state, created_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (opportunity_id, "BTCUSDT", "OPEN", NOW_ISO, NOW_ISO, "{}"),
            )
        for index_name in (
            "opportunity_conditions_opportunity",
            "opportunity_conditions_type",
            "opportunity_conditions_evaluable",
            "opportunity_conditions_condition_id",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        connection.execute(
            "ALTER TABLE opportunity_conditions RENAME TO opportunity_conditions_v18_new"
        )
        # The pre-fix v18 shape: a single-column, globally unique condition id.
        connection.execute(
            """
            CREATE TABLE opportunity_conditions (
                condition_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
                kind TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                params_json TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                required_data_json TEXT NOT NULL,
                evaluable INTEGER NOT NULL,
                unavailable_reason TEXT,
                last_value TEXT,
                last_evaluated_at TEXT,
                last_result INTEGER
            )
            """
        )
        for condition_id, opportunity_id in (
            ("C-1", "OPP-OLD-A"),
            ("C-2", "OPP-OLD-B"),
        ):
            connection.execute(
                "INSERT INTO opportunity_conditions VALUES "
                "(?, ?, 'TRIGGER', 1, 'PRICE_TOUCH_ZONE', '{}', ?, ?, '[\"MARK_PRICE\"]', 1, "
                "NULL, NULL, NULL, NULL)",
                (condition_id, opportunity_id, NOW_ISO, FUTURE),
            )
        connection.execute("DROP TABLE opportunity_conditions_v18_new")
        connection.commit()
    finally:
        connection.close()

    build_container(settings, initialize=True, clock=FixedClock(NOW))

    connection = sqlite3.connect(db)
    try:
        info = connection.execute("PRAGMA table_info(opportunity_conditions)").fetchall()
        primary_key = tuple(
            row[1] for row in sorted((row for row in info if int(row[5])), key=lambda row: row[5])
        )
        rows = connection.execute(
            "SELECT opportunity_id, condition_id FROM opportunity_conditions "
            "ORDER BY opportunity_id"
        ).fetchall()
        # The rebuild restored the composite identity: the same condition id
        # may now exist for a second opportunity.
        connection.execute(
            "INSERT INTO opportunity_conditions "
            "(condition_id, opportunity_id, kind, seq, type, params_json, window_start, "
            "window_end, required_data_json, evaluable) "
            "VALUES ('C-1', 'OPP-OLD-B', 'TRIGGER', 2, 'PRICE_TOUCH_ZONE', '{}', ?, ?, "
            "'[\"MARK_PRICE\"]', 1)",
            (NOW_ISO, FUTURE),
        )
        connection.commit()
    finally:
        connection.close()

    assert primary_key == ("opportunity_id", "condition_id")
    assert rows == [("OPP-OLD-A", "C-1"), ("OPP-OLD-B", "C-2")]


_PRE_FIX_V18_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (18, 't');
CREATE TABLE opportunities (
    opportunity_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('OPEN','TRIGGERED','EXPIRED','INVALIDATED')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
-- The pre-fix-1 v18 shape: a single-column, globally unique condition id and
-- no queryable ``expires_at`` column on opportunities.
CREATE TABLE opportunity_conditions (
    condition_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    kind TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    required_data_json TEXT NOT NULL,
    evaluable INTEGER NOT NULL,
    unavailable_reason TEXT,
    last_value TEXT,
    last_evaluated_at TEXT,
    last_result INTEGER
);
"""


def _pre_fix_v18_payload(
    opportunity_id: str, *, expires_at: str, condition_ids: tuple[str, ...]
) -> str:
    return json.dumps(
        {
            "opportunity_id": opportunity_id,
            "expires_at": expires_at,
            "trigger_conditions": [
                {
                    "condition_id": condition_id,
                    "type": "PRICE_TOUCH_ZONE",
                    "params": {"price_low": "99", "price_high": "101"},
                    "window": {"start": NOW_ISO, "end": FUTURE},
                    "required_data": ["MARK_PRICE"],
                    "evaluable": True,
                }
                for condition_id in condition_ids
            ],
            "invalidation_conditions": [],
        }
    )


def _build_pre_fix_v18_database(
    settings: Settings, *, payloads: dict[str, str], condition_rows: list[tuple[str, str]]
) -> None:
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_PRE_FIX_V18_SCHEMA)
        for opportunity_id, payload in payloads.items():
            connection.execute(
                "INSERT INTO opportunities VALUES (?, ?, 'OPEN', ?, ?, ?)",
                (opportunity_id, "BTCUSDT", NOW_ISO, NOW_ISO, payload),
            )
        for condition_id, opportunity_id in condition_rows:
            connection.execute(
                "INSERT INTO opportunity_conditions VALUES "
                "(?, ?, 'TRIGGER', 1, 'PRICE_TOUCH_ZONE', '{}', ?, ?, "
                "'[\"MARK_PRICE\"]', 1, NULL, NULL, NULL, NULL)",
                (condition_id, opportunity_id, NOW_ISO, FUTURE),
            )
        connection.commit()
    finally:
        connection.close()


def test_pre_fix_v18_database_without_expires_at_boots_and_rebuilds(
    tmp_path: Path,
) -> None:
    """A real pre-fix-1 v18 DB must upgrade instead of aborting at boot.

    The expiry backfill used to leave an implicit transaction open, so the
    composite-PK rebuild raised "cannot start a transaction within a
    transaction" and the daemon never booted.
    """
    settings = _settings(tmp_path)
    _build_pre_fix_v18_database(
        settings,
        payloads={
            "OPP-PRE-A": _pre_fix_v18_payload(
                "OPP-PRE-A",
                expires_at="2026-11-06T17:30:00+05:30",
                condition_ids=("C-1",),
            )
        },
        condition_rows=[("C-1", "OPP-PRE-A")],
    )

    build_container(settings, initialize=True, clock=FixedClock(NOW))

    connection = sqlite3.connect(database_path(settings))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunities)")}
        info = connection.execute("PRAGMA table_info(opportunity_conditions)").fetchall()
        primary_key = tuple(
            row[1] for row in sorted((row for row in info if int(row[5])), key=lambda row: row[5])
        )
        expires_at = connection.execute(
            "SELECT expires_at FROM opportunities WHERE opportunity_id = 'OPP-PRE-A'"
        ).fetchone()[0]
        condition_ids = [
            row[0]
            for row in connection.execute(
                "SELECT condition_id FROM opportunity_conditions WHERE opportunity_id = 'OPP-PRE-A'"
            )
        ]
    finally:
        connection.close()

    assert "expires_at" in columns
    assert primary_key == ("opportunity_id", "condition_id")
    # The expiry backfill normalised the offset form to canonical UTC millis.
    assert expires_at == "2026-11-06T12:00:00.000Z"
    assert condition_ids == ["C-1"]


def test_pre_fix_v18_rebuild_restores_rows_lost_to_the_global_key(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _build_pre_fix_v18_database(
        settings,
        payloads={
            "OPP-PRE-A": _pre_fix_v18_payload("OPP-PRE-A", expires_at=PAST, condition_ids=("C-1",)),
            "OPP-PRE-B": _pre_fix_v18_payload("OPP-PRE-B", expires_at=PAST, condition_ids=("C-1",)),
        },
        # The pre-fix global PK let opportunity B's C-1 replace A's mirror row.
        condition_rows=[("C-1", "OPP-PRE-B")],
    )

    build_container(settings, initialize=True, clock=FixedClock(NOW))

    connection = sqlite3.connect(database_path(settings))
    try:
        rows = set(
            connection.execute(
                "SELECT opportunity_id, condition_id FROM opportunity_conditions"
            ).fetchall()
        )
    finally:
        connection.close()

    # Both payloads re-materialise the mirror row, so A's C-1 is restored.
    assert rows == {("OPP-PRE-A", "C-1"), ("OPP-PRE-B", "C-1")}


def test_a_failed_condition_insert_rolls_back_the_payload_and_event(
    tmp_path: Path,
) -> None:
    container = build_container(_settings(tmp_path), initialize=True, clock=FixedClock(NOW))
    record = OpportunityRecord(
        opportunity_id="OPP-ROLLBACK",
        symbol="BTCUSDT",
        state="OPEN",
        created_at=NOW_ISO,
        updated_at=NOW_ISO,
        payload_json=json.dumps({"opportunity_id": "OPP-ROLLBACK"}),
    )
    event = OpportunityEventRecord(
        opportunity_id="OPP-ROLLBACK",
        seq=1,
        at=NOW_ISO,
        from_state=None,
        to_state="OPEN",
        reason_code="CREATED",
        note="",
    )
    orphan = ConditionRecord(
        condition_id="C-1",
        opportunity_id="OPP-MISSING",
        kind="TRIGGER",
        seq=1,
        type="PRICE_TOUCH_ZONE",
        params_json="{}",
        window_start=NOW_ISO,
        window_end=FUTURE,
        required_data_json='["MARK_PRICE"]',
        evaluable=True,
    )

    with pytest.raises(sqlite3.IntegrityError):
        container.store.save_opportunity(record, event, (orphan,))

    assert container.store.get_opportunity("OPP-ROLLBACK") is None
    assert container.store.list_opportunity_events("OPP-ROLLBACK") == ()


# -- expiry and display evaluation -----------------------------------------


def test_expire_due_moves_only_past_due_opportunities(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, expired_candidate = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    fresh = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=FUTURE,
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-2",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    expired = service.expire_due(NOW_ISO)

    assert [item.opportunity_id for item in expired] == [expired_candidate.opportunity_id]
    assert service.show(expired_candidate.opportunity_id).state.value == "EXPIRED"
    assert service.show(fresh.opportunity_id).state.value == "OPEN"
    timeline = service.timeline(expired_candidate.opportunity_id)
    assert timeline[-1]["to_state"] == "EXPIRED"
    assert timeline[-1]["reason_code"] == "EXPIRED"


def test_due_rows_are_selected_in_sql_without_starvation(tmp_path: Path) -> None:
    container, due = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    for index in range(1005):
        service.create_from_decision(
            opportunity_draft=_draft(
                triggers=[
                    _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
                ],
                invalidations=[],
                expires_at=FUTURE,
            ),
            subject={"symbol": "BTCUSDT"},
            decision_id=f"DEC-FRESH-{index}",
            scan_id="SCAN-1",
            report_id="RPT-1",
            confidence=0.7,
            provenance=_provenance(),
            data_quality=_data_quality(),
            at=NOW_ISO,
            available_data=_full_available(),
        )

    due_rows = container.store.list_due_opportunities(NOW_ISO)

    assert [row.opportunity_id for row in due_rows] == [due.opportunity_id]
    assert [item.opportunity_id for item in service.expire_due(NOW_ISO)] == [due.opportunity_id]


def test_expires_at_is_normalised_and_each_due_row_expires_once(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container, first = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            # Same instant as PAST, written with a +05:30 offset.
            expires_at="2026-01-01T05:30:00+05:30",
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    second = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            # Same instant as PAST, written without fractional seconds.
            expires_at="2026-01-01T00:00:00Z",
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-PRECISION",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    connection = sqlite3.connect(database_path(settings))
    try:
        stored = dict(
            connection.execute("SELECT opportunity_id, expires_at FROM opportunities").fetchall()
        )
    finally:
        connection.close()

    assert stored == {first.opportunity_id: PAST, second.opportunity_id: PAST}
    due = container.store.list_due_opportunities("2026-01-01T05:30:00+05:30")
    assert {row.opportunity_id for row in due} == {
        first.opportunity_id,
        second.opportunity_id,
    }

    expired = service.expire_due("2026-01-01T05:30:00+05:30")

    assert {row.opportunity_id for row in expired} == {
        first.opportunity_id,
        second.opportunity_id,
    }
    assert service.expire_due(NOW_ISO) == ()


def test_expire_due_skips_a_row_that_lost_the_transition_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, first = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    second = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-RACED",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )
    # Simulate a concurrent trigger winning between selection and expiry.
    service.mark_triggered(second.opportunity_id, trigger_id="TRG-RACE", at=NOW_ISO)
    raced_record = container.store.get_opportunity(second.opportunity_id)
    assert raced_record is not None
    first_record = container.store.get_opportunity(first.opportunity_id)
    assert first_record is not None
    monkeypatch.setattr(
        container.store,
        "list_due_opportunities",
        lambda now, limit=1000: (raced_record, first_record),
    )

    expired = service.expire_due(NOW_ISO)

    assert [item.opportunity_id for item in expired] == [first.opportunity_id]
    assert service.show(second.opportunity_id).state.value == "TRIGGERED"


def test_expired_opportunity_is_never_displayed_as_triggering(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    service.expire(opportunity.opportunity_id, at=NOW_ISO)

    display = service.evaluate_display(opportunity.opportunity_id)

    assert display["would_trigger"] is False
    assert display["state"] == "EXPIRED"
    assert all(item["met"] is False for item in display["evaluations"])
    assert all(item["note"] == "opportunity is expired" for item in display["evaluations"])


def test_expired_display_preserves_the_persisted_unavailable_reason(
    tmp_path: Path,
) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[_invalidation("MANUAL")],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    service.expire(opportunity.opportunity_id, at=NOW_ISO)

    display = service.evaluate_display(opportunity.opportunity_id)
    manual = next(
        item
        for item in display["evaluations"]
        if item["condition_id"] == opportunity.invalidation_conditions[0].condition_id
    )

    assert manual["evaluable"] is False
    assert manual["met"] is False
    assert manual["unavailable_reason"] == "manual invalidation is code-only"


def test_open_opportunity_past_expiry_is_displayed_as_expired_without_mutating(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    record = container.store.get_opportunity(opportunity.opportunity_id)
    assert record is not None
    before = record.payload_json

    display = service.evaluate_display(opportunity.opportunity_id)

    assert display["state"] == "OPEN"
    assert display["would_trigger"] is False
    assert all(item["met"] is False for item in display["evaluations"])
    assert all(item["note"] == "opportunity is expired" for item in display["evaluations"])
    assert container.store.get_opportunity(opportunity.opportunity_id).payload_json == before


def test_no_llm_facing_or_route_code_path_changes_opportunity_state() -> None:
    from oft.api.routes import opportunities as opportunities_api
    from oft.application import analysis_service, llm_orchestrator, llm_validation
    from oft.cli.commands import opp as opp_cli

    forbidden = ("mark_triggered", "transition_opportunity", ".transition(", ".invalidate(")
    for module in (
        analysis_service,
        llm_orchestrator,
        llm_validation,
        opportunities_api,
        opp_cli,
    ):
        source = inspect.getsource(module)
        for token in forbidden:
            assert token not in source, f"{module.__name__} references {token!r}"


def test_display_evaluation_is_side_effect_free(tmp_path: Path) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    record = container.store.get_opportunity(opportunity.opportunity_id)
    assert record is not None
    before = record.payload_json

    display = service.evaluate_display(opportunity.opportunity_id)

    assert display["would_trigger"] is True
    assert container.store.get_opportunity(opportunity.opportunity_id).payload_json == before
    rows = container.store.list_conditions(opportunity.opportunity_id)
    assert all(row.last_evaluated_at is None for row in rows)


def test_show_unknown_opportunity_raises_not_found(tmp_path: Path) -> None:
    container = build_container(_settings(tmp_path), initialize=True, clock=FixedClock(NOW))
    service = OpportunityService(store=container.store, clock=container.clock)

    with pytest.raises(OpportunityNotFound, match="does not exist"):
        service.show("OPP-MISSING")


def test_timeline_falls_back_to_the_payload_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute(
            "DELETE FROM opportunity_events WHERE opportunity_id = ?",
            (opportunity.opportunity_id,),
        )
        connection.commit()
    finally:
        connection.close()
    service = OpportunityService(store=container.store, clock=container.clock)

    timeline = service.timeline(opportunity.opportunity_id)

    assert [entry["to_state"] for entry in timeline] == ["OPEN"]


def test_code_only_invalidate_and_trigger_transitions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, first = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    second = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-2",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    invalidated = service.invalidate(first.opportunity_id, reason="human reject", at=NOW_ISO)
    triggered = service.mark_triggered(second.opportunity_id, trigger_id="TRG-1", at=NOW_ISO)

    assert invalidated.state.value == "INVALIDATED"
    assert triggered.state.value == "TRIGGERED"
    assert service.show(first.opportunity_id).state_history[-1].reason_code == "REJECTED"


def test_a_lost_race_surfaces_as_a_transition_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, opportunity = _create(
        _settings(tmp_path),
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    monkeypatch.setattr(container.store, "transition_opportunity", lambda *a, **k: False)

    with pytest.raises(OpportunityTransitionError):
        service.expire(opportunity.opportunity_id, at=NOW_ISO)


def test_display_evaluation_without_a_report_id_is_unevaluable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        report_id="",
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)

    display = service.evaluate_display(opportunity.opportunity_id)

    assert display["would_trigger"] is False
    assert display["data_quality"] == "MISSING"
    assert all(item["evaluable"] is False for item in display["evaluations"])
    assert all(
        item["unavailable_reason"] == "report unavailable" for item in display["evaluations"]
    )


def test_display_evaluation_with_malformed_snapshot_is_unevaluable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.store.save_scan(_scan_record())
    container.store.save_report(
        ReportRecord(
            report_id="RPT-BAD",
            scan_id="SCAN-1",
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at=NOW_ISO,
            metrics_manifest_json="[]",
            llm_decision_id=None,
            narrative_ref=None,
            data_quality="COMPLETE",
            snapshot_json="{not-json",
            metrics_json="{}",
        )
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    opportunity = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-BAD",
        scan_id="SCAN-1",
        report_id="RPT-BAD",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    display = service.evaluate_display(opportunity.opportunity_id)

    assert display["would_trigger"] is False
    assert display["data_quality"] == "MISSING"
    assert all(item["evaluable"] is False for item in display["evaluations"])
    assert all(
        item["unavailable_reason"] == "report unavailable" for item in display["evaluations"]
    )


def test_invalid_invalidation_parameters_are_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    service = OpportunityService(store=container.store, clock=container.clock)

    with pytest.raises(ConditionDraftError) as excinfo:
        service.create_from_decision(
            opportunity_draft=_draft(
                triggers=[
                    _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
                ],
                invalidations=[_invalidation("DELTA_ABOVE", threshold=None)],
            ),
            subject={"symbol": "BTCUSDT"},
            decision_id="DEC-BAD",
            scan_id="SCAN-1",
            report_id="RPT-1",
            confidence=0.7,
            provenance=_provenance(),
            data_quality=_data_quality(),
            at=NOW_ISO,
            available_data=_full_available(),
        )

    assert excinfo.value.field == "threshold"
    assert container.store.list_opportunities() == ()


# -- API --------------------------------------------------------------------


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_api_exposes_read_only_conditions_and_export(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, opportunity = _create(
        settings,
        draft=_multi_condition_draft(),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        listed = client.get("/api/v1/opportunities?state=open", headers=auth)
        assert listed.status_code == 200
        assert [item["opportunity_id"] for item in listed.json()["data"]["items"]] == [
            opportunity.opportunity_id
        ]

        shown = client.get(f"/api/v1/opportunities/{opportunity.opportunity_id}", headers=auth)
        assert shown.status_code == 200
        assert [item["condition_id"] for item in shown.json()["data"]["trigger_conditions"]] == [
            "C-1",
            "C-2",
            "C-3",
        ]

        conditions = client.get(
            f"/api/v1/opportunities/{opportunity.opportunity_id}/conditions",
            headers=auth,
        )
        assert conditions.status_code == 200, conditions.text
        data = conditions.json()["data"]
        assert len(data["conditions"]) == 5
        assert len(data["evaluations"]) == 5
        assert data["would_trigger"] is False
        liquidation = next(
            item for item in data["evaluations"] if item["type"] == "LIQUIDATION_NOTIONAL_ABOVE"
        )
        assert liquidation["met"] is False
        assert liquidation["evaluable"] is False

        exported = client.get(
            f"/api/v1/opportunities/{opportunity.opportunity_id}/export",
            headers=auth,
        )
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]
        assert exported.json()["data"]["opportunity_id"] == opportunity.opportunity_id


def test_api_conditions_are_evaluable_from_the_recorded_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101"),
                _trigger("DELTA_ABOVE", draft_id="c2", threshold="0.5", lookback_bars=2),
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/opportunities/{opportunity.opportunity_id}/conditions",
            headers=_auth(app),
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["would_trigger"] is True
    assert data["data_quality"] == "COMPLETE"
    assert all(item["evaluable"] for item in data["evaluations"])
    assert all(item["met"] for item in data["evaluations"])


def test_api_conditions_without_a_report_never_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        report_id="RPT-MISSING",
    )
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/opportunities/{opportunity.opportunity_id}/conditions",
            headers=_auth(app),
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["would_trigger"] is False
    assert data["data_quality"] == "MISSING"
    assert all(item["evaluable"] is False for item in data["evaluations"])
    assert all(item["unavailable_reason"] == "report unavailable" for item in data["evaluations"])


def test_unknown_opportunity_condition_and_export_routes_return_not_found(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        conditions = client.get("/api/v1/opportunities/OPP-MISSING/conditions", headers=auth)
        exported = client.get("/api/v1/opportunities/OPP-MISSING/export", headers=auth)

    assert conditions.status_code == 404
    assert conditions.json()["error"]["code"] == "NOT_FOUND"
    assert exported.status_code == 404
    assert exported.json()["error"]["code"] == "NOT_FOUND"


def test_opportunity_routes_are_read_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
        ),
        snapshot=_snapshot(),
    )
    app = create_app(settings, clock=FixedClock(NOW))

    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/v1/opportunities"):
            assert getattr(route, "methods", set()) <= {"GET", "HEAD"}, path

    with TestClient(app) as client:
        rejected = client.post(
            f"/api/v1/opportunities/{opportunity.opportunity_id}/conditions",
            headers=_auth(app),
        )
    assert rejected.status_code == 405


# -- CLI --------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def ledger_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Settings, str, str]]:
    port = _free_port()
    settings = _settings(tmp_path, port=port)
    container, opportunity = _create(
        settings,
        draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[_invalidation("PRICE_CROSS_BELOW", price="69000.00")],
        ),
        snapshot=_snapshot(),
        available=_full_available(),
    )
    service = OpportunityService(store=container.store, clock=container.clock)
    due = service.create_from_decision(
        opportunity_draft=_draft(
            triggers=[
                _trigger("PRICE_TOUCH_ZONE", draft_id="c1", price_low="99", price_high="101")
            ],
            invalidations=[],
            expires_at=PAST,
        ),
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-DUE",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=0.7,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=_full_available(),
    )

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("daemon thread exited before startup")
        if time.monotonic() > deadline:
            raise RuntimeError("daemon did not start within 10s")
        time.sleep(0.02)

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    try:
        yield settings, opportunity.opportunity_id, due.opportunity_id
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _cli_json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cli_conditions_evaluate_export_and_expire_due(
    ledger_daemon: tuple[Settings, str, str], tmp_path: Path
) -> None:
    _settings_value, opportunity_id, due_id = ledger_daemon
    runner = CliRunner()

    listed = _cli_json(runner.invoke(cli_app, ["opp", "list", "--state", "open", "--json"]))
    assert opportunity_id in [item["opportunity_id"] for item in listed["data"]["items"]]

    conditions = _cli_json(runner.invoke(cli_app, ["opp", "conditions", opportunity_id, "--json"]))
    assert [item["condition_id"] for item in conditions["data"]["conditions"]] == [
        "C-1",
        "I-1",
    ]

    evaluated = _cli_json(runner.invoke(cli_app, ["opp", "evaluate", opportunity_id, "--json"]))
    assert evaluated["data"]["would_trigger"] is True

    text_evaluate = runner.invoke(cli_app, ["opp", "evaluate", opportunity_id])
    assert text_evaluate.exit_code == 0, text_evaluate.output
    assert "would_trigger=True" in text_evaluate.output

    out = tmp_path / "opportunity.json"
    exported = runner.invoke(cli_app, ["opp", "export", opportunity_id, "--out", str(out)])
    assert exported.exit_code == 0, exported.output
    envelope = json.loads(out.read_text(encoding="utf-8"))
    assert envelope["data"]["opportunity_id"] == opportunity_id

    expired = _cli_json(runner.invoke(cli_app, ["opp", "expire-due", "--json"]))
    assert [item["opportunity_id"] for item in expired["data"]["items"]] == [due_id]

    after = _cli_json(runner.invoke(cli_app, ["opp", "list", "--state", "expired", "--json"]))
    assert [item["opportunity_id"] for item in after["data"]["items"]] == [due_id]


def test_cli_show_renders_unavailable_reasons() -> None:
    from oft.cli.commands.opp import _render_opportunity

    rendered = _render_opportunity(
        {
            "opportunity_id": "OPP-1",
            "symbol": "BTCUSDT",
            "trigger_conditions": [
                {
                    "condition_id": "C-1",
                    "type": "LIQUIDATION_NOTIONAL_ABOVE",
                    "params": {"threshold": "1000"},
                    "evaluable": False,
                    "unavailable_reason": "historical liquidations are unavailable",
                }
            ],
            "invalidation_conditions": [
                {
                    "condition_id": "I-1",
                    "type": "TIME_WINDOW",
                    "params": {"original_type": "MANUAL"},
                    "evaluable": False,
                    "unavailable_reason": "manual invalidation is code-only",
                }
            ],
        }
    )

    assert "C-1 LIQUIDATION_NOTIONAL_ABOVE evaluable=False" in rendered
    assert "historical liquidations are unavailable" in rendered
    assert "I-1 TIME_WINDOW evaluable=False" in rendered
    assert "manual invalidation is code-only" in rendered


def test_cli_has_no_manual_trigger_or_state_override_command() -> None:
    from oft.cli.commands.opp import opp_app

    opp_names = {command.name for command in opp_app.registered_commands}
    assert opp_names == {
        "list",
        "show",
        "timeline",
        "conditions",
        "evaluate",
        "export",
        "expire-due",
    }
    assert "invalidate" not in opp_names
    assert "trigger" not in opp_names
