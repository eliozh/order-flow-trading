"""Restart drain: queued rows survive, sent rows are never re-sent, no duplicates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import TELEGRAM_LAST_SEND_AT_KEY
from oft.ports.store import TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _record(event_key: str, *, state: str = "queued", chat_id: int = 1001) -> TelegramOutboxRecord:
    return TelegramOutboxRecord(
        event_key=event_key,
        kind="query_reply",
        chat_id=chat_id,
        reply_to_message_id=None,
        payload_json=json.dumps({"text": event_key}),
        priority=0,
        state=state,  # type: ignore[arg-type]
        attempts=0,
        next_attempt_at=NOW_TEXT,
        message_id=None,
        last_error_code=None,
        created_at=NOW_TEXT,
        sent_at=NOW_TEXT if state == "sent" else None,
    )


def _dispatcher(store: SqliteStore, port: TelegramFake) -> OutboxDispatcher:
    return OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
    )


def test_a_fresh_dispatcher_drains_queued_rows_but_never_resends_sent_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("queued-1"))
    store.append_telegram_outbox(_record("already-sent", state="sent", chat_id=2002))
    store.append_telegram_outbox(_record("already-failed", state="failed", chat_id=3003))
    port = TelegramFake(sent_message_id_start=800)

    # A brand-new process (dispatcher) starts: only the queued row drains.
    assert _dispatcher(store, port).dispatch_once() == 1

    queued = store.get_telegram_outbox("queued-1")
    already_sent = store.get_telegram_outbox("already-sent")
    already_failed = store.get_telegram_outbox("already-failed")
    assert queued is not None and already_sent is not None and already_failed is not None
    assert (queued.state, queued.message_id) == ("sent", 801)
    assert already_sent.message_id is None
    assert already_failed.message_id is None
    assert [call["text"] for call in port.send_calls] == ["queued-1"]
    assert store.get_system_state(TELEGRAM_LAST_SEND_AT_KEY) == NOW_TEXT


def test_a_second_restart_does_not_resend_anything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("queued-1"))
    port = TelegramFake()
    assert _dispatcher(store, port).dispatch_once() == 1

    assert _dispatcher(store, TelegramFake()).dispatch_once() == 0
    assert len(port.send_calls) == 1


def test_a_crash_after_claim_is_drained_after_the_lease_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("queued-1"))
    # Simulate a claim that committed and then the process died.
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute(
            "UPDATE telegram_outbox SET state = 'sending', next_attempt_at = ? WHERE event_key = ?",
            ("2026-11-17T09:00:00.000Z", "queued-1"),
        )
        connection.commit()
    port = TelegramFake()

    assert _dispatcher(store, port).dispatch_once() == 1

    drained = store.get_telegram_outbox("queued-1")
    assert drained is not None
    assert drained.state == "sent"
    assert len(port.send_calls) == 1


def test_a_duplicate_event_key_after_restart_is_still_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("queued-1"))
    # The poller replayed the update after a crash: the unique key coalesces it.
    assert store.append_telegram_outbox(_record("queued-1")) is False

    port = TelegramFake()
    assert _dispatcher(store, port).dispatch_once() == 1
    assert store.count_telegram_receipts() == 1
    assert len(port.send_calls) == 1
