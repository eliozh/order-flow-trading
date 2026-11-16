"""Real-SQLite checks for the report-scoped overlay reads (slice 40 fix round).

The chart server must never list the newest ledger rows globally and filter in
Python: an older report would be starved by newer rows for other reports. These
tests seed a real ``SqliteStore`` with more rows than the overlay read bound and
prove the linked report still resolves its zone and markers.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from oft.adapters.sqlite_store import SqliteStore
from oft.application.chart_service import derive_overlays
from oft.config import Settings
from oft.ports.store import ReportRecord

REPORT_ID = "RPT-ROUTE"
SYMBOL = "BTCUSDT"
NEWER_ROWS = 250


def _report(report_id: str = REPORT_ID) -> ReportRecord:
    return ReportRecord(
        report_id=report_id,
        scan_id="SCAN-1",
        symbol=SYMBOL,
        timeframe="4h",
        generated_at="2026-10-08T00:00:00.000Z",
        metrics_manifest_json="[]",
        llm_decision_id=None,
        narrative_ref=None,
        data_quality="COMPLETE",
        snapshot_json="{}",
        metrics_json="{}",
    )


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _opportunity_payload(opportunity_id: str, report_id: str) -> str:
    return json.dumps(
        {
            "opportunity_id": opportunity_id,
            "symbol": SYMBOL,
            "side": "LONG",
            "state": "TRIGGERED",
            "zone": {"low": "69000.00", "high": "71000.00"},
            "confidence": 0.72,
            "source": {"report_id": report_id},
        }
    )


def _position_payload(position_id: str, trigger_id: str) -> str:
    return json.dumps(
        {
            "position_id": position_id,
            "symbol": SYMBOL,
            "source_trigger_id": trigger_id,
            "avg_entry_price": "70000.00",
            "stop_price": "69000.00",
            "target_prices": ["72000.00"],
            "exit_price": None,
        }
    )


def _insert_opportunity(
    connection: sqlite3.Connection,
    opportunity_id: str,
    report_id: str,
    created_at: str,
    *,
    payload_json: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO opportunities "
        "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
        "VALUES (?, ?, 'TRIGGERED', ?, ?, ?, NULL)",
        (
            opportunity_id,
            SYMBOL,
            created_at,
            created_at,
            payload_json
            if payload_json is not None
            else _opportunity_payload(opportunity_id, report_id),
        ),
    )


def _insert_trigger(
    connection: sqlite3.Connection,
    trigger_id: str,
    opportunity_id: str,
    triggered_at: str,
) -> None:
    connection.execute(
        "INSERT INTO trigger_events "
        "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, payload_json) "
        "VALUES (?, ?, ?, 'ACCEPTED', ?, ?, '{}')",
        (trigger_id, opportunity_id, SYMBOL, f"KEY-{trigger_id}", triggered_at),
    )


def _insert_position(
    connection: sqlite3.Connection,
    position_id: str,
    trigger_id: str,
    opened_at: str,
    *,
    payload_json: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO positions "
        "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
        "VALUES (?, 'ACC-PAPER', 'PAPER', ?, 'OPEN', ?, ?)",
        (
            position_id,
            SYMBOL,
            opened_at,
            payload_json
            if payload_json is not None
            else _position_payload(position_id, trigger_id),
        ),
    )


def _seed_starving_rows(store: SqliteStore) -> None:
    """The linked report's rows are older than the whole read bound."""
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, environment, currency, initial_balance, balance, updated_at) "
            "VALUES ('ACC-PAPER', 'PAPER', 'USDT', '10000.00', '10000.00', '2020-01-01T00:00:00Z')"
        )
        _insert_opportunity(connection, "OPP-LINKED", REPORT_ID, "2020-01-01T00:00:00.000Z")
        _insert_trigger(connection, "TRG-LINKED", "OPP-LINKED", "2020-01-01T00:00:00.000Z")
        _insert_position(connection, "POS-LINKED", "TRG-LINKED", "2020-01-01T00:00:00.000Z")

        for index in range(NEWER_ROWS):
            other_report = f"RPT-OTHER-{index:03d}"
            stamp = f"2026-10-{1 + index // 100:02d}T{index % 24:02d}:00:00.000Z"
            _insert_opportunity(connection, f"OPP-NEW-{index:03d}", other_report, stamp)
            _insert_trigger(connection, f"TRG-NEW-{index:03d}", f"OPP-NEW-{index:03d}", stamp)
            _insert_position(connection, f"POS-NEW-{index:03d}", f"TRG-NEW-{index:03d}", stamp)


def test_scoped_reads_find_the_linked_zone_and_markers_behind_newer_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_starving_rows(store)

    zones, markers, truncated = derive_overlays(_report(), store)

    assert [zone["opportunity_id"] for zone in zones] == ["OPP-LINKED"]
    assert zones[0]["label"] == "OPP-LINKED · LONG · 0.72"
    marker_keys = {(marker["kind"], marker["price"], marker["position_id"]) for marker in markers}
    assert ("entry", "70000.00", "POS-LINKED") in marker_keys
    assert ("stop", "69000.00", "POS-LINKED") in marker_keys
    assert ("target", "72000.00", "POS-LINKED") in marker_keys
    assert truncated is False


def test_scoped_query_does_not_raise_on_corrupt_payload_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, environment, currency, initial_balance, balance, updated_at) "
            "VALUES ('ACC-PAPER', 'PAPER', 'USDT', '10000.00', '10000.00', '2020-01-01T00:00:00Z')"
        )
        _insert_opportunity(
            connection,
            "OPP-CORRUPT",
            REPORT_ID,
            "2020-01-01T00:00:00.000Z",
            payload_json="{not json",
        )
        _insert_opportunity(connection, "OPP-VALID", REPORT_ID, "2020-01-02T00:00:00.000Z")
        _insert_trigger(connection, "TRG-VALID", "OPP-VALID", "2020-01-02T00:00:00.000Z")
        _insert_position(
            connection,
            "POS-CORRUPT",
            "TRG-VALID",
            "2020-01-03T00:00:00.000Z",
            payload_json="{not json",
        )
        _insert_position(connection, "POS-VALID", "TRG-VALID", "2020-01-04T00:00:00.000Z")

    zones, markers, _ = derive_overlays(_report(), store)

    assert [zone["opportunity_id"] for zone in zones] == ["OPP-VALID"]
    assert {marker["position_id"] for marker in markers} == {"POS-VALID"}


def test_other_report_and_symbol_rows_stay_excluded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        _insert_opportunity(connection, "OPP-LINKED", REPORT_ID, "2020-01-01T00:00:00.000Z")
        _insert_opportunity(connection, "OPP-OTHER", "RPT-OTHER", "2020-01-01T00:00:00.000Z")
        payload: dict[str, Any] = json.loads(_opportunity_payload("OPP-FOREIGN", REPORT_ID))
        payload["symbol"] = "ETHUSDT"
        _insert_opportunity(
            connection,
            "OPP-FOREIGN",
            REPORT_ID,
            "2020-01-01T00:00:00.000Z",
            payload_json=json.dumps(payload),
        )

    zones, _, _ = derive_overlays(_report(), store)

    assert [zone["opportunity_id"] for zone in zones] == ["OPP-LINKED"]
