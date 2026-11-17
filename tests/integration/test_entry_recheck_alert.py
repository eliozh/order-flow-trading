"""Entry re-check pushes: one read-only alert per persisted decision."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.notifications import NotificationService
from oft.application.opportunity_service import OpportunityService
from oft.application.settings_service import SettingsService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import ENTRY_RECHECK_ALERT_KEY
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    ConfigRecord,
    DecisionRecord,
    OpportunityRecord,
    TriggerEventRecord,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
TRIGGER_ID = "TRG-1"
OPPORTUNITY_ID = "OPP-1"
DECISION_ID = "DEC-ENTRY-1"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(_settings(tmp_path))
    store.initialize()
    return store


def _insert_opportunity(store: SqliteStore) -> None:
    payload = {
        "opportunity_id": OPPORTUNITY_ID,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "zone": {"low": "69000.00", "high": "71000.00"},
        "entry_preference": "MARKET",
        "trigger_conditions": [],
        "invalidation_conditions": [],
        "expires_at": "2099-01-01T00:00:00.000Z",
        "thesis": "SECRET_THESIS",
        "confidence": 0.72,
        "risk_request": {},
        "source": {"report_id": "RPT-1"},
        "provenance": {},
        "data_quality": {},
        "state_history": [
            {
                "from_state": None,
                "to_state": "OPEN",
                "at": NOW_TEXT,
                "reason_code": "CREATED",
                "note": "test",
            }
        ],
    }
    store.save_opportunity(
        OpportunityRecord(
            opportunity_id=OPPORTUNITY_ID,
            symbol="BTCUSDT",
            state="OPEN",
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )


def _trigger_payload(*, status: str = "RECHECKED") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "trigger_id": TRIGGER_ID,
        "opportunity_id": OPPORTUNITY_ID,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "environment": "PAPER",
        "triggered_at": NOW_TEXT,
        "trigger_source": "CODE",
        "condition_results": [],
        "snapshot": {"market_snapshot_id": "SNAP-1", "data_quality": "COMPLETE"},
        "status": status,
        "entry_decision_id": DECISION_ID,
        "risk_verdict": None,
        "human_action": None,
        "idempotency_key": "TRG-OPP-1-SNAP",
        "rejection_reason": None,
        "created_order_id": None,
    }


def _insert_trigger(store: SqliteStore, *, status: str = "RECHECKED") -> None:
    store.save_trigger_event(
        TriggerEventRecord(
            trigger_id=TRIGGER_ID,
            opportunity_id=OPPORTUNITY_ID,
            symbol="BTCUSDT",
            status=status,
            idempotency_key="TRG-OPP-1-SNAP",
            triggered_at=NOW_TEXT,
            payload_json=json.dumps(_trigger_payload(status=status), sort_keys=True),
            snapshot_hash="hash",
            as_of=NOW_TEXT,
            data_quality="COMPLETE",
            condition_count=0,
        )
    )


def _insert_decision(store: SqliteStore, *, decision: str = "ENTER_LONG") -> None:
    envelope = {
        "decision_id": DECISION_ID,
        "decision_type": "ENTRY_RECHECK",
        "validation_status": "VALID",
        "confidence": 0.8,
        "entry_recheck": {"decision": decision, "order_intent": None},
        # Free text that must never reach the phone alert.
        "rationale": "SECRET_RAW_PROMPT_AND_PROVIDER_BODY",
        "provenance": {"provider": "test", "model": "test-model"},
    }
    store.save_decision(
        DecisionRecord(
            decision_id=DECISION_ID,
            role="ENTRY_RECHECK",
            decision_type="ENTRY_RECHECK",
            validation_status="VALID",
            raw_output_hash="hash",
            persisted_at=NOW_TEXT,
            payload_json=json.dumps(envelope, sort_keys=True),
        )
    )


class _ReadyChannel:
    def status_state(self) -> object:
        return SimpleNamespace(state="READY")

    def allowlist(self) -> object:
        return SimpleNamespace(chat_ids=(1001,))


def _notifications(store: SqliteStore, settings: Settings) -> NotificationService:
    return NotificationService(
        store=store,
        clock=FixedClock(NOW),
        channel=_ReadyChannel(),  # type: ignore[arg-type]
        settings_service=SettingsService(store=store, clock=FixedClock(NOW), settings=settings),
    )


def _service(
    store: SqliteStore,
    settings: Settings,
    *,
    notifications: NotificationService | None = None,
) -> EntryService:
    return EntryService(
        store=store,
        clock=FixedClock(NOW),
        provider=None,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=store, clock=FixedClock(NOW)),
        notifications=notifications,
    )


def _text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]


def _prepared(
    tmp_path: Path,
    *,
    decision: str = "ENTER_LONG",
) -> tuple[SqliteStore, Settings, NotificationService]:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    _insert_opportunity(store)
    _insert_trigger(store)
    _insert_decision(store, decision=decision)
    return store, settings, _notifications(store, settings)


def test_a_recheck_awaiting_human_action_enqueues_one_alert_with_decision_and_confidence(
    tmp_path: Path,
) -> None:
    store, settings, notifications = _prepared(tmp_path)
    service = _service(store, settings, notifications=notifications)

    card = service.recheck(TRIGGER_ID)

    assert card["llm"]["decision"] == "ENTER_LONG"
    assert card["status"] == "RECHECKED"
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == f"entry:{TRIGGER_ID}:{DECISION_ID}"
    assert receipt.kind == "entry_recheck"
    text = _text(receipt)
    assert text.splitlines()[0] == "入场复核 · BTCUSDT · LONG · 置信度 0.8"
    assert f"decision_id: {DECISION_ID}" in text
    assert f"trigger_id: {TRIGGER_ID}" in text
    assert "请在桌面端或 CLI 处理。" in text


def test_reusing_the_persisted_decision_does_not_send_twice(tmp_path: Path) -> None:
    store, settings, notifications = _prepared(tmp_path)
    service = _service(store, settings, notifications=notifications)

    first = service.recheck(TRIGGER_ID)
    second = service.recheck(TRIGGER_ID)

    assert first["entry_decision_id"] == second["entry_decision_id"] == DECISION_ID
    assert len(store.list_telegram_receipts()) == 1


def test_the_entry_alert_says_desktop_or_cli_and_has_no_approval_shortcut(
    tmp_path: Path,
) -> None:
    store, settings, notifications = _prepared(tmp_path)
    service = _service(store, settings, notifications=notifications)

    service.recheck(TRIGGER_ID)

    text = _text(store.list_telegram_receipts()[0])
    assert "请在桌面端或 CLI 处理。" in text.splitlines()
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://"):
        assert forbidden not in text
    assert "SECRET_RAW_PROMPT_AND_PROVIDER_BODY" not in text


def test_a_disabled_entry_toggle_suppresses_with_no_orphan_row(tmp_path: Path) -> None:
    store, settings, _unused = _prepared(tmp_path)
    store.set_config(
        ConfigRecord(
            key=ENTRY_RECHECK_ALERT_KEY,
            value_json=json.dumps(False),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )
    service = _service(store, settings, notifications=_notifications(store, settings))

    service.recheck(TRIGGER_ID)

    assert store.list_telegram_receipts() == ()


def test_an_enqueue_failure_leaves_the_recheck_decision_and_trigger_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, settings, notifications = _prepared(tmp_path)
    service = _service(store, settings, notifications=notifications)

    def _boom(self: SqliteStore, record: object) -> bool:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _boom)

    card = service.recheck(TRIGGER_ID)

    assert card["status"] == "RECHECKED"
    trigger = store.get_trigger_event(TRIGGER_ID)
    assert trigger is not None and trigger.status == "RECHECKED"
    assert json.loads(trigger.payload_json)["status"] == "RECHECKED"
    assert store.get_decision(DECISION_ID) is not None
    assert store.list_telegram_receipts() == ()


def test_no_entry_sends_no_entry_alert_and_a_reject_sends_the_invalidation_alert(
    tmp_path: Path,
) -> None:
    store, settings, notifications = _prepared(tmp_path, decision="NO_ENTRY")
    service = _service(store, settings, notifications=notifications)

    card = service.recheck(TRIGGER_ID)

    assert card["llm"]["decision"] == "NO_ENTRY"
    assert store.list_telegram_receipts() == ()

    # The same trigger then rejects; the idea invalidation is the alert that
    # fires instead of an entry alert.
    service.reject(TRIGGER_ID, reason="NO_ENTRY")

    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "opportunity_invalidated"
    assert receipts[0].event_key == "opp:OPP-1:INVALIDATED:2"
    stored = OpportunityService(store=store, clock=FixedClock(NOW)).show(OPPORTUNITY_ID)
    assert stored.state.value == "INVALIDATED"
