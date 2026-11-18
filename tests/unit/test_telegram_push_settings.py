"""Read-only Telegram push toggles: defaults, persisted values, fail-closed decoding."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.settings_service import SettingsService
from oft.clock import FixedClock
from oft.config import ConfigError, Settings
from oft.domain.telegram import (
    ENTRY_RECHECK_ALERT_KEY,
    SCAN_FAILED_ALERT_KEY,
    SCAN_REPORT_PUSH_KEY,
    TRIGGER_ALERT_KEY,
)
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[SqliteStore, SettingsService]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store, SettingsService(store=store, clock=FixedClock(NOW), settings=settings)


def _set_raw(store: SqliteStore, key: str, value_json: str) -> None:
    store.set_config(
        ConfigRecord(
            key=key,
            value_json=value_json,
            updated_at="2026-11-17T09:30:00.000Z",
            updated_by="test",
        )
    )


def test_absent_toggles_read_their_documented_defaults(tmp_path: Path) -> None:
    _store, service = _service(tmp_path)

    assert service.read_scan_report_push() is True
    assert service.read_failed_scan_alert() is False
    assert service.read_trigger_alert() is True
    assert service.read_entry_recheck_alert() is True


def test_persisted_toggles_are_honoured(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    _set_raw(store, SCAN_REPORT_PUSH_KEY, json.dumps(False))
    _set_raw(store, SCAN_FAILED_ALERT_KEY, json.dumps(True))
    _set_raw(store, TRIGGER_ALERT_KEY, json.dumps(False))
    _set_raw(store, ENTRY_RECHECK_ALERT_KEY, json.dumps(False))

    assert service.read_scan_report_push() is False
    assert service.read_failed_scan_alert() is True
    assert service.read_trigger_alert() is False
    assert service.read_entry_recheck_alert() is False


@pytest.mark.parametrize("reader", ["read_trigger_alert", "read_entry_recheck_alert"])
def test_an_unreadable_lifecycle_toggle_raises_config_error(tmp_path: Path, reader: str) -> None:
    store, service = _service(tmp_path)
    key = TRIGGER_ALERT_KEY if reader == "read_trigger_alert" else ENTRY_RECHECK_ALERT_KEY
    _set_raw(store, key, '"not-a-boolean"')

    with pytest.raises(ConfigError):
        getattr(service, reader)()


@pytest.mark.parametrize("raw", ["not-json", "{}", "[]"])
def test_malformed_toggle_json_raises_config_error(tmp_path: Path, raw: str) -> None:
    store, service = _service(tmp_path)
    _set_raw(store, SCAN_REPORT_PUSH_KEY, raw)

    with pytest.raises(ConfigError):
        service.read_scan_report_push()


@pytest.mark.parametrize("raw", ["1", "0", '"yes"', "null"])
def test_a_non_bool_toggle_raises_config_error(tmp_path: Path, raw: str) -> None:
    store, service = _service(tmp_path)
    _set_raw(store, SCAN_FAILED_ALERT_KEY, raw)

    with pytest.raises(ConfigError):
        service.read_failed_scan_alert()
