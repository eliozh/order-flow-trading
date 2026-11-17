"""Opportunity lifecycle pushes: one idempotent alert per durable transition."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.notifications import NotificationService
from oft.application.opportunity_service import OpportunityService
from oft.application.settings_service import SettingsService
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import MAX_OPPORTUNITY_ALERT_ITEMS, TRIGGER_ALERT_KEY
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot
from oft.ports.store import ConfigRecord, OpportunityRecord

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(_settings(tmp_path))
    store.initialize()
    return store


def _snapshot(symbol: str = "BTCUSDT", price: str = "70000.00") -> TriggerSnapshot:
    return TriggerSnapshot(
        symbol=symbol,
        as_of=NOW_TEXT,
        last_price=Decimal(price),
        previous_price=Decimal("69900.00"),
        tick_size=Decimal("0.10"),
        data_quality="COMPLETE",
        snapshot_id=f"SNAP-{symbol}",
    )


class _FixedProvider:
    def __init__(self, snapshot: TriggerSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot_for(self, opportunity: object) -> TriggerSnapshot:
        return self._snapshot


class _DynamicProvider:
    def snapshot_for(self, opportunity: Any) -> TriggerSnapshot:
        return _snapshot(opportunity.symbol)


def _insert_opportunity(
    store: SqliteStore,
    opportunity_id: str = "OPP-1",
    *,
    symbol: str = "BTCUSDT",
) -> str:
    payload = {
        "opportunity_id": opportunity_id,
        "symbol": symbol,
        "side": "LONG",
        "state": "OPEN",
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "zone": {"low": "69000.00", "high": "71000.00"},
        "entry_preference": "MARKET",
        "trigger_conditions": [
            {
                "condition_id": "C-1",
                "draft_id": "c1",
                "type": "PRICE_TOUCH_ZONE",
                "params": {"price_low": "69900.00", "price_high": "70100.00"},
                "window": WINDOW,
                "required_data": ["CONTRACT_SPECS", "MARK_PRICE"],
                "evaluable": True,
            }
        ],
        "invalidation_conditions": [
            {
                "condition_id": "I-1",
                "draft_id": "i1",
                "type": "PRICE_CROSS_BELOW",
                "params": {"price": "68000.00"},
                "window": WINDOW,
                "required_data": ["MARK_PRICE"],
                "evaluable": True,
            }
        ],
        "expires_at": "2099-01-01T00:00:00.000Z",
        "thesis": "SECRET_THESIS_PROMPT_TEXT",
        "confidence": 0.72,
        "risk_request": {},
        "source": {"report_id": f"RPT-{opportunity_id}"},
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
            opportunity_id=opportunity_id,
            symbol=symbol,
            state="OPEN",
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )
    return opportunity_id


class _ReadyChannel:
    def status_state(self) -> object:
        return SimpleNamespace(state="READY")

    def allowlist(self) -> object:
        return SimpleNamespace(chat_ids=(1001,))


def _notifications(
    store: SqliteStore, settings: Settings, *, clock: Any | None = None
) -> NotificationService:
    resolved_clock = clock or FixedClock(NOW)
    return NotificationService(
        store=store,
        clock=resolved_clock,
        channel=_ReadyChannel(),  # type: ignore[arg-type]
        settings_service=SettingsService(store=store, clock=resolved_clock, settings=settings),
    )


def _trigger_service(
    store: SqliteStore,
    settings: Settings,
    *,
    provider: Any | None = None,
    notifications: NotificationService | None = None,
) -> TriggerService:
    return TriggerService(
        store=store,
        clock=FixedClock(NOW),
        provider=provider or _FixedProvider(_snapshot()),
        notifications=notifications,
    )


def _text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]


def test_one_durable_triggered_transition_enqueues_exactly_one_outbox_row(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _trigger_service(store, settings, notifications=_notifications(store, settings))

    run = service.evaluate(opportunity_id)

    assert run.created is True
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == "opp:OPP-1:TRIGGERED:2"
    assert receipt.kind == "opportunity_triggered"
    assert receipt.chat_id == 1001
    # The outbox row carries only the rendered text; the allowlisted event
    # payload is asserted at the builder boundary in the unit suite.
    assert set(json.loads(receipt.payload_json)) == {"text"}
    text = _text(receipt)
    assert text.splitlines()[0] == "机会触发 · BTCUSDT · LONG · 置信度 0.72"
    assert "state: TRIGGERED" in text
    assert "zone: 69000 - 71000 USDT" in text
    assert ("trigger: C-1 PRICE_TOUCH_ZONE observed 70000 USDT threshold 69900.00-70100.00") in text
    assert "invalidation: I-1 PRICE_CROSS_BELOW 68000 USDT" in text
    assert "state_version: 2" in text
    assert text.splitlines()[-1] == "/opportunities"
    assert "SECRET_THESIS_PROMPT_TEXT" not in text


def test_replaying_the_same_transition_and_a_duplicate_tick_produce_no_second_row(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _trigger_service(store, settings, notifications=_notifications(store, settings))

    assert service.evaluate(opportunity_id).created is True
    assert service.evaluate(opportunity_id).created is False
    duplicate_tick = service.evaluate_all(NOW_TEXT)

    assert duplicate_tick.triggered == 0
    assert len(store.list_telegram_receipts()) == 1
    assert len(store.list_trigger_events()) == 1


def test_a_concurrent_evaluator_tick_cannot_double_enqueue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    services = [
        _trigger_service(store, settings, notifications=_notifications(store, settings))
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def _run(service: TriggerService) -> None:
        barrier.wait(timeout=10)
        results.append(service.evaluate(opportunity_id))

    threads = [threading.Thread(target=_run, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert sum(1 for run in results if run.created) == 1
    assert len(store.list_trigger_events()) == 1
    # The outbox's unique event_key is the second line of defence.
    assert len(store.list_telegram_receipts()) == 1


def test_an_invalidated_transition_enqueues_one_row_with_the_reason_code(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = OpportunityService(
        store=store, clock=FixedClock(NOW), notifications=_notifications(store, settings)
    )

    service.invalidate(opportunity_id, reason="HUMAN_REJECT_SECRET_NOTE")

    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == "opp:OPP-1:INVALIDATED:2"
    assert receipt.kind == "opportunity_invalidated"
    text = _text(receipt)
    assert text.splitlines()[0] == "机会失效 · BTCUSDT · LONG"
    assert "reason_code: REJECTED" in text
    assert "state: INVALIDATED" in text
    assert "state_version: 2" in text
    assert "HUMAN_REJECT_SECRET_NOTE" not in text


def test_a_disabled_trigger_toggle_suppresses_at_enqueue_with_no_orphan_row(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    store.set_config(
        ConfigRecord(
            key=TRIGGER_ALERT_KEY,
            value_json=json.dumps(False),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )
    service = _trigger_service(store, settings, notifications=_notifications(store, settings))

    run = service.evaluate(opportunity_id)

    assert run.created is True
    # Neither a queued nor a failed outbox row exists.
    assert store.list_telegram_receipts() == ()


def test_a_raising_outbox_append_leaves_the_ledger_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)

    def _boom(self: SqliteStore, record: object) -> bool:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _boom)
    service = _trigger_service(store, settings, notifications=_notifications(store, settings))

    run = service.evaluate(opportunity_id)

    assert run.created is True
    stored = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    assert stored.state.value == "TRIGGERED"
    assert len(store.list_trigger_events()) == 1
    assert store.list_telegram_receipts() == ()


def test_a_multi_symbol_burst_tick_enqueues_one_bounded_coalesced_message(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    total = MAX_OPPORTUNITY_ALERT_ITEMS + 2
    for index in range(total):
        _insert_opportunity(store, f"OPP-{index}", symbol=f"SYM{index}USDT")
    service = _trigger_service(
        store,
        settings,
        provider=_DynamicProvider(),
        notifications=_notifications(store, settings),
    )

    outcome = service.evaluate_all(NOW_TEXT)

    assert outcome.triggered == total
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.kind == "opportunity_triggered_burst"
    assert receipt.event_key.startswith("opp-burst:TRIGGERED:")
    text = _text(receipt)
    lines = text.splitlines()
    assert lines[0] == f"机会触发 {total} 个"
    assert len([line for line in lines if line.startswith("- ")]) == MAX_OPPORTUNITY_ALERT_ITEMS
    assert "另有 2 个" in lines
    assert lines[-1] == "请使用 /opportunities 查看"


def test_the_dispatcher_sends_each_transition_exactly_once_via_the_fake_transport(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    _insert_opportunity(store)
    _insert_opportunity(store, "OPP-2", symbol="ETHUSDT")
    service = _trigger_service(
        store,
        settings,
        provider=_DynamicProvider(),
        notifications=_notifications(store, settings),
    )
    service.evaluate("OPP-1")
    service.evaluate("OPP-2")
    assert len(store.list_telegram_receipts()) == 2

    fake = TelegramFake()
    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: fake,
    )

    assert dispatcher.dispatch_once() == 2
    assert dispatcher.dispatch_once() == 0
    assert len(fake.sent) == 2
    assert {message["text"].splitlines()[0] for message in fake.sent} == {
        "机会触发 · BTCUSDT · LONG · 置信度 0.72",
        "机会触发 · ETHUSDT · LONG · 置信度 0.72",
    }
    assert all(receipt.state == "sent" for receipt in store.list_telegram_receipts())
