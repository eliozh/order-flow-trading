"""Persistence, audit, and migration tests for the ``config`` table (slice 32)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.settings import SCAN_PERIOD_KEY
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


@pytest.fixture(autouse=True)
def _clear_scan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _service(tmp_path: Path) -> tuple[SettingsService, Any]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    service = SettingsService(
        store=container.store,
        clock=container.clock,
    )
    return service, container


def _audit_body(record: Any) -> dict[str, Any]:
    return json.loads(record.payload_json)


def test_a_save_writes_one_row_and_exactly_one_audit_event(tmp_path: Path) -> None:
    service, container = _service(tmp_path)

    saved = service.update_scan_period("8h", actor="web", reason="desk preference")

    assert saved.value == "8h"
    assert saved.source.value == "user"
    rows = container.store.list_config()
    assert len(rows) == 1
    assert rows[0].key == SCAN_PERIOD_KEY
    assert json.loads(rows[0].value_json) == "8h"
    assert rows[0].updated_at == "2026-11-06T10:00:00.000Z"
    assert rows[0].updated_by == "web"

    audits = container.store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "CONFIG_UPDATED"
    body = _audit_body(audits[0])
    assert body["actor"] == {"kind": "USER", "id": "web"}
    assert body["subject"] == {"kind": "CONFIG", "id": "scan.period"}
    assert body["payload"] == {
        "key": "scan.period",
        "before": "4h",
        "after": "8h",
        "source_before": "default",
        "source_after": "user",
        "reason": "desk preference",
    }
    assert audits[0].seq == 1


def test_a_same_value_save_is_a_deterministic_no_op(tmp_path: Path) -> None:
    service, container = _service(tmp_path)

    first = service.update_scan_period("8h", actor="web")
    second = service.update_scan_period("8h", actor="web")

    assert first.value == second.value == "8h"
    assert len(container.store.list_config()) == 1
    assert len(container.store.list_audit_events()) == 1

    # Saving the current effective default is also a no-op: no row is created.
    service.update_scan_period("8h", actor="web")
    fresh_service, fresh_container = _service(tmp_path / "other")
    fresh_service.update_scan_period("4h", actor="web")
    assert fresh_container.store.list_config() == ()
    assert fresh_container.store.list_audit_events() == ()


def test_a_second_change_chains_the_next_audit_event(tmp_path: Path) -> None:
    service, container = _service(tmp_path)

    service.update_scan_period("8h", actor="web")
    service.update_scan_period("4h", actor="cli", reason="revert")

    audits = container.store.list_audit_events()
    assert len(audits) == 2
    newest = _audit_body(audits[0])
    assert newest["actor"] == {"kind": "USER", "id": "cli"}
    assert newest["payload"]["before"] == "8h"
    assert newest["payload"]["after"] == "4h"
    assert newest["payload"]["source_before"] == "user"
    assert newest["payload"]["source_after"] == "user"
    assert newest["payload"]["reason"] == "revert"
    assert audits[0].previous_hash == audits[1].event_hash


def test_an_audit_insert_failure_rolls_back_the_config_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = SettingsService(store=store, clock=FixedClock(NOW))

    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("simulated audit failure")

    monkeypatch.setattr(store, "_insert_audit_event", _fail)

    with pytest.raises(sqlite3.OperationalError):
        service.update_scan_period("8h", actor="web")

    fresh = SqliteStore(settings)
    assert fresh.list_config() == ()
    assert fresh.list_audit_events() == ()


def test_config_rows_round_trip_without_secret_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("8h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="cli",
        )
    )

    record = store.get_config(SCAN_PERIOD_KEY)
    assert record is not None
    assert json.loads(record.value_json) == "8h"
    assert record.updated_by == "cli"
    assert [item.key for item in store.list_config()] == [SCAN_PERIOD_KEY]
    raw = (settings.data_dir / "oft.db").read_bytes()
    assert b"api_key" not in raw
    assert b"secret" not in raw


def _config_shape(settings: Settings) -> dict[str, Any]:
    connection = sqlite3.connect(database_path(settings))
    try:
        columns = [tuple(row) for row in connection.execute("PRAGMA table_info(config)")]
        indexes = sorted(
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'config'"
            )
        )
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        connection.close()
    return {"columns": columns, "indexes": indexes, "version": version}


def test_fresh_database_matches_a_migrated_v25_database(tmp_path: Path) -> None:
    fresh_settings = _settings(tmp_path / "fresh")
    SqliteStore(fresh_settings).initialize()
    fresh = _config_shape(fresh_settings)

    migrated_settings = _settings(tmp_path / "migrated")
    migrated_store = SqliteStore(migrated_settings)
    migrated_store.initialize()
    connection = sqlite3.connect(database_path(migrated_settings))
    try:
        connection.execute("DROP TABLE config")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.commit()
    finally:
        connection.close()

    migrated_store.initialize()
    migrated = _config_shape(migrated_settings)

    assert fresh == migrated
    assert fresh["version"] == 26
    column_names = [column[1] for column in fresh["columns"]]
    assert column_names == ["key", "value_json", "updated_at", "updated_by"]
    assert any(name == "config_updated_at" for name, _sql in fresh["indexes"])
    # Re-running the migration is idempotent and preserves the table.
    migrated_store.initialize()
    assert _config_shape(migrated_settings) == migrated
