"""Telegram allowlist persistence: validation, atomicity, audit, and no-op."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.settings_service import (
    MAX_TELEGRAM_ALLOWED_USER_IDS,
    MAX_TELEGRAM_CHAT_IDS,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    TELEGRAM_ALLOWLIST_UPDATED,
    TELEGRAM_CHAT_IDS_KEY,
    SettingsService,
)
from oft.clock import FixedClock
from oft.config import ConfigError, Settings
from oft.domain.settings import SettingValidationError
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[SqliteStore, SettingsService]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store, SettingsService(store=store, clock=FixedClock(NOW), settings=settings)


def _audits(store: SqliteStore) -> list[str]:
    return [event.event_type for event in store.list_audit_events_ascending()]


def test_an_unset_allowlist_reads_as_empty(tmp_path: Path) -> None:
    _store, service = _service(tmp_path)

    allowlist = service.read_telegram_allowlist()

    assert allowlist.chat_ids == ()
    assert allowlist.allowed_user_ids == ()
    assert allowlist.updated_at is None


def test_update_sorts_dedupes_and_writes_exactly_one_audit(tmp_path: Path) -> None:
    store, service = _service(tmp_path)

    result = service.update_telegram_allowlist(
        [30, 10, 30, 20], [5, 3, 5], actor="cli", reason="initial setup"
    )

    assert result.chat_ids == (10, 20, 30)
    assert result.allowed_user_ids == (3, 5)
    assert json.loads(store.get_config(TELEGRAM_CHAT_IDS_KEY).value_json) == [10, 20, 30]  # type: ignore[union-attr]
    assert json.loads(store.get_config(TELEGRAM_ALLOWED_USER_IDS_KEY).value_json) == [3, 5]  # type: ignore[union-attr]
    events = store.list_audit_events_ascending()
    assert [event.event_type for event in events] == [TELEGRAM_ALLOWLIST_UPDATED]
    payload = json.loads(events[0].payload_json)
    assert payload["payload"]["before"] == {"chat_ids": [], "allowed_user_ids": []}
    assert payload["payload"]["after"] == {"chat_ids": [10, 20, 30], "allowed_user_ids": [3, 5]}
    assert payload["payload"]["reason"] == "initial setup"
    assert result.updated_by == "cli"
    assert result.updated_at == "2026-11-17T09:30:00.000Z"


def test_a_repeated_value_is_a_noop_with_no_new_rows_or_audit(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    service.update_telegram_allowlist([10], [20], actor="web")
    before = store.get_config(TELEGRAM_CHAT_IDS_KEY)
    audit_count = len(store.list_audit_events_ascending())

    result = service.update_telegram_allowlist([10], [20], actor="web")

    assert result.chat_ids == (10,)
    assert store.get_config(TELEGRAM_CHAT_IDS_KEY) == before
    assert len(store.list_audit_events_ascending()) == audit_count


@pytest.mark.parametrize("value", [True, False, "10", 10.0, 0])
def test_invalid_chat_ids_are_rejected_with_a_field_detail(tmp_path: Path, value: object) -> None:
    _store, service = _service(tmp_path)

    with pytest.raises(SettingValidationError) as caught:
        service.update_telegram_allowlist([value], [], actor="web")  # type: ignore[list-item]

    assert caught.value.details
    assert caught.value.details[0]["field"] == "chat_ids"


def test_a_negative_group_chat_id_round_trips(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    group_id = -1001234567890

    result = service.update_telegram_allowlist([group_id], [20], actor="web")

    assert result.chat_ids == (group_id,)
    assert json.loads(store.get_config(TELEGRAM_CHAT_IDS_KEY).value_json) == [group_id]  # type: ignore[union-attr]
    assert service.read_telegram_allowlist().chat_ids == (group_id,)


@pytest.mark.parametrize("value", [True, "20", 20.0, -5, 0])
def test_invalid_user_ids_are_rejected_with_a_field_detail(tmp_path: Path, value: object) -> None:
    _store, service = _service(tmp_path)

    with pytest.raises(SettingValidationError) as caught:
        service.update_telegram_allowlist([10], [value], actor="web")  # type: ignore[list-item]

    assert caught.value.details[0]["field"] == "allowed_user_ids"


def test_bounds_are_enforced_for_both_lists(tmp_path: Path) -> None:
    _store, service = _service(tmp_path)

    with pytest.raises(SettingValidationError):
        service.update_telegram_allowlist(
            list(range(1, MAX_TELEGRAM_CHAT_IDS + 2)), [], actor="web"
        )
    with pytest.raises(SettingValidationError):
        service.update_telegram_allowlist(
            [10], list(range(1, MAX_TELEGRAM_ALLOWED_USER_IDS + 2)), actor="web"
        )


def test_non_list_payloads_are_rejected(tmp_path: Path) -> None:
    _store, service = _service(tmp_path)

    with pytest.raises(SettingValidationError):
        service.update_telegram_allowlist(10, [], actor="web")  # type: ignore[arg-type]


def test_a_malformed_persisted_value_is_a_typed_config_error(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    store.set_config(
        record=ConfigRecord(
            key=TELEGRAM_CHAT_IDS_KEY,
            value_json="not-json",
            updated_at="2026-11-17T09:30:00.000Z",
            updated_by="test",
        )
    )

    with pytest.raises(ConfigError):
        service.read_telegram_allowlist()
