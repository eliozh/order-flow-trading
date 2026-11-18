"""Entry re-check pushes: one read-only alert per durable decision.

These tests drive the fresh re-check path (the branch that persists a new LLM
decision), because that is the only path allowed to enqueue the "awaiting human
action" push. Reusing a persisted decision is a pure read.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.adapters.sqlite_store import SqliteStore
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.notifications import NotificationService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.settings_service import SettingsService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import ENTRY_RECHECK_ALERT_KEY
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole
from oft.ports.store import ConfigRecord, TelegramOutboxRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
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


def _prepared(tmp_path: Path, *, entry_fixture: str | None = None) -> tuple[Any, Any, str]:
    """A fully wired opportunity/trigger whose re-check runs the fresh LLM path."""
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    provider = container.llm_provider
    if entry_fixture is not None:
        provider = LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.ENTRY_RECHECK: entry_fixture},
        )
    assert provider is not None
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
        notifications=_notifications(container.store, settings),
    )
    return container, service, run.trigger.trigger_id


def _text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]


def test_a_fresh_recheck_enqueues_one_alert_with_decision_and_confidence(
    tmp_path: Path,
) -> None:
    container, service, trigger_id = _prepared(tmp_path)

    card = service.recheck(trigger_id)

    assert card["status"] == "RECHECKED"
    assert card["llm"]["decision"] == "ENTER_LONG"
    receipts = container.store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == f"entry:{trigger_id}:{card['decision_id']}"
    assert receipt.kind == "entry_recheck"
    text = _text(receipt)
    assert text.splitlines()[0] == "入场复核 · BTCUSDT · LONG · 置信度 0.68"
    assert f"decision_id: {card['decision_id']}" in text
    assert f"trigger_id: {trigger_id}" in text
    assert "请在桌面端或 CLI 处理。" in text


def test_reusing_the_persisted_decision_does_not_send_twice(tmp_path: Path) -> None:
    container, service, trigger_id = _prepared(tmp_path)

    first = service.recheck(trigger_id)
    second = service.recheck(trigger_id)

    assert first["decision_id"] == second["decision_id"]
    assert second["persisted"] is True
    assert len(container.store.list_telegram_receipts()) == 1


def test_the_persisted_read_path_never_enqueues_an_outbox_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, trigger_id = _prepared(tmp_path)
    service.recheck(trigger_id)

    # A read endpoint must not be the trigger for a push. The original alert is
    # already in the outbox, so re-reading the persisted decision could only
    # re-insert the same key (a no-op today) and would re-send it after outbox
    # retention purges the row. Assert the read path performs no write at all.
    appends: list[TelegramOutboxRecord] = []
    original = SqliteStore.append_telegram_outbox

    def _spy(self: SqliteStore, record: TelegramOutboxRecord) -> bool:
        appends.append(record)
        return original(self, record)

    monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _spy)

    card = service.recheck(trigger_id)

    assert card["persisted"] is True
    assert appends == []
    assert len(container.store.list_telegram_receipts()) == 1


def test_the_entry_alert_says_desktop_or_cli_and_has_no_approval_shortcut(
    tmp_path: Path,
) -> None:
    container, service, trigger_id = _prepared(tmp_path)

    service.recheck(trigger_id)

    text = _text(container.store.list_telegram_receipts()[0])
    assert "请在桌面端或 CLI 处理。" in text.splitlines()
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://"):
        assert forbidden not in text
    # The stub fixture supplies rationale and provider text; neither may reach
    # the push.
    assert "The zone touch still holds and the stop is intact." not in text
    assert "stub" not in text.lower()


def test_a_disabled_entry_toggle_suppresses_with_no_orphan_row(tmp_path: Path) -> None:
    container, service, trigger_id = _prepared(tmp_path)
    container.store.set_config(
        ConfigRecord(
            key=ENTRY_RECHECK_ALERT_KEY,
            value_json=json.dumps(False),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )

    service.recheck(trigger_id)

    assert container.store.list_telegram_receipts() == ()


def test_an_enqueue_failure_leaves_the_fresh_decision_and_trigger_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, trigger_id = _prepared(tmp_path)

    def _boom(self: SqliteStore, record: object) -> bool:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _boom)

    card = service.recheck(trigger_id)

    assert card["status"] == "RECHECKED"
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None and trigger.status == "RECHECKED"
    stored = json.loads(trigger.payload_json)
    assert stored["status"] == "RECHECKED"
    assert stored["entry_decision_id"] == card["decision_id"]
    assert container.store.get_decision(card["decision_id"]) is not None
    audits = [
        event
        for event in container.store.list_audit_events(limit=50)
        if event.event_type == "ENTRY_RECHECK"
    ]
    assert len(audits) == 1
    assert container.store.list_telegram_receipts() == ()


def test_accept_bootstraps_the_recheck_without_queueing_an_entry_alert(
    tmp_path: Path,
) -> None:
    container, service, trigger_id = _prepared(tmp_path)
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    before = json.loads(trigger.payload_json)
    # A freshly triggered opportunity has no decision yet: accept() must
    # bootstrap one through the fresh re-check path under test.
    assert before["status"] == "PENDING_RECHECK"
    assert before.get("entry_decision_id") is None

    result = service.accept(trigger_id, idempotency_key="accept-fresh-decision")

    assert result.status == "ACCEPTED"
    assert result.order is not None
    receipts = container.store.list_telegram_receipts()
    # Accept performs the action the alert would ask for, so no
    # "awaiting human action" push may be queued by the bootstrap re-check.
    assert not any(receipt.event_key.startswith("entry:") for receipt in receipts)


def test_no_entry_sends_no_entry_alert_and_a_reject_sends_the_invalidation_alert(
    tmp_path: Path,
) -> None:
    container, service, trigger_id = _prepared(
        tmp_path, entry_fixture="entry_recheck_no_entry.json"
    )

    card = service.recheck(trigger_id)

    assert card["llm"]["decision"] == "NO_ENTRY"
    assert container.store.list_telegram_receipts() == ()

    # The same trigger then rejects; the idea invalidation is the alert that
    # fires instead of an entry alert.
    service.reject(trigger_id, reason="NO_ENTRY")

    receipts = container.store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "opportunity_invalidated"
    assert ":INVALIDATED:" in receipts[0].event_key
    assert not any(receipt.event_key.startswith("entry:") for receipt in receipts)
