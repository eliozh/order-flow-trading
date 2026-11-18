"""Queue bound, coalescing, global sender bound, and per-chat ordering."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oft.adapters import sqlite_store as store_module
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.settings_service import TelegramAllowlistSettings
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_service import TelegramService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    TELEGRAM_OUTBOX_OVERFLOW_KEY,
    TELEGRAM_QUEUE_FULL,
    TelegramUpdate,
)
from oft.ports.store import TelegramOutboxRecord
from oft.ports.telegram import TELEGRAM_UPSTREAM_ERROR, TelegramError

NOW_TEXT = "2026-11-17T09:30:00.000Z"
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _record(
    event_key: str,
    *,
    chat_id: int = 1001,
    priority: int = 0,
    state: str = "queued",
    created_at: str = NOW_TEXT,
) -> TelegramOutboxRecord:
    return TelegramOutboxRecord(
        event_key=event_key,
        kind="query_reply",
        chat_id=chat_id,
        reply_to_message_id=None,
        payload_json=json.dumps({"text": event_key}),
        priority=priority,
        state=state,  # type: ignore[arg-type]
        attempts=0,
        next_attempt_at=created_at,
        message_id=None,
        last_error_code=None,
        created_at=created_at,
        sent_at=created_at if state == "sent" else None,
    )


def test_the_pending_queue_has_a_global_cap_and_records_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 2)
    store = _store(tmp_path)

    assert store.append_telegram_outbox(_record("first", chat_id=1001)) is True
    assert store.append_telegram_outbox(_record("second", chat_id=2002)) is True
    assert store.append_telegram_outbox(_record("third", chat_id=3003)) is False

    assert store.telegram_receipt_counts() == {
        "queued": 2,
        "sending": 0,
        "sent": 0,
        "failed": 0,
    }
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) == TELEGRAM_QUEUE_FULL


def test_the_pending_queue_has_a_per_chat_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 1000)
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING_PER_CHAT", 1)
    store = _store(tmp_path)

    assert store.append_telegram_outbox(_record("first", chat_id=1001)) is True
    assert store.append_telegram_outbox(_record("second", chat_id=1001)) is False
    # Another chat is unaffected by one chat's saturated queue.
    assert store.append_telegram_outbox(_record("third", chat_id=2002)) is True
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) == TELEGRAM_QUEUE_FULL


def test_terminal_rows_do_not_count_toward_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 1)
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("old-sent", chat_id=1001, state="sent"))
    store.append_telegram_outbox(_record("old-failed", chat_id=1002, state="failed"))

    assert store.append_telegram_outbox(_record("queued", chat_id=1003)) is True


def test_duplicate_event_keys_are_coalesced_and_do_not_grow_the_queue(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.append_telegram_outbox(_record("same")) is True
    assert store.append_telegram_outbox(_record("same")) is False
    assert store.count_telegram_receipts() == 1


def test_a_later_higher_priority_message_cannot_jump_an_earlier_same_chat_message(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("earlier", priority=-5))
    store.append_telegram_outbox(_record("later", priority=5))

    claimed = store.claim_due_telegram_outbox(now=NOW_TEXT, limit=10)

    # Same-chat rows are claimed in enqueue order, regardless of priority; the
    # dispatcher then sends them one at a time in that order.
    assert [item.event_key for item in claimed] == ["earlier", "later"]


def test_an_earlier_same_chat_row_in_backoff_blocks_later_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("earlier", priority=-5))
    store.append_telegram_outbox(_record("later", priority=5))
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute(
            "UPDATE telegram_outbox SET next_attempt_at = ? WHERE event_key = ?",
            ("2026-11-17T09:31:00.000Z", "earlier"),
        )
        connection.commit()

    claimed = store.claim_due_telegram_outbox(now=NOW_TEXT, limit=10)

    # The later row may not overtake the earlier one while it is backing off.
    assert claimed == ()


def test_different_chats_proceed_independently_in_one_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("a", chat_id=1001))
    store.append_telegram_outbox(_record("b", chat_id=2002))

    claimed = store.claim_due_telegram_outbox(now=NOW_TEXT, limit=10)

    assert {item.event_key for item in claimed} == {"a", "b"}


def test_the_global_sender_batch_bounds_one_tick(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(3):
        store.append_telegram_outbox(_record(f"msg-{index}", chat_id=1000 + index))

    claimed = store.claim_due_telegram_outbox(now=NOW_TEXT, limit=2)

    assert len(claimed) == 2


def test_per_chat_status_counts_only_that_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("a-queued", chat_id=1001))
    store.append_telegram_outbox(_record("a-failed", chat_id=1001, state="failed"))
    store.append_telegram_outbox(_record("b-sent", chat_id=2002, state="sent"))
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute(
            "UPDATE telegram_outbox SET last_error_code = ? WHERE event_key = ?",
            ("TELEGRAM_UPSTREAM_ERROR", "a-failed"),
        )
        connection.commit()

    statuses = {status.chat_id: status for status in store.telegram_chat_statuses()}

    assert statuses[1001].queued == 1
    assert statuses[1001].failed == 1
    assert statuses[1001].last_error_code == "TELEGRAM_UPSTREAM_ERROR"
    assert statuses[2002].sent == 1
    assert statuses[2002].failed == 0
    assert store.last_telegram_sent_at() == NOW_TEXT


def test_the_dispatcher_sends_a_same_chat_burst_sequentially_in_enqueue_order(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("first", priority=-5))
    store.append_telegram_outbox(_record("second", priority=5))
    port = TelegramFake()

    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
    )

    assert dispatcher.dispatch_once() == 2
    assert [call["text"] for call in port.send_calls] == ["first", "second"]


def test_a_failed_earlier_send_releases_later_same_chat_rows_without_counting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("first"))
    store.append_telegram_outbox(_record("second"))
    port = TelegramFake(fail_send_times=1)

    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
    )

    assert dispatcher.dispatch_once() == 0

    first = store.get_telegram_outbox("first")
    second = store.get_telegram_outbox("second")
    assert first is not None and second is not None
    assert (first.state, first.attempts) == ("queued", 1)
    # The later row was claimed in the same tick but never sent or charged an
    # attempt: it waits for the earlier row, preserving per-chat order.
    assert (second.state, second.attempts, second.last_error_code) == ("queued", 0, None)
    assert second.next_attempt_at == NOW_TEXT


def test_one_chat_failing_does_not_block_another_chat_in_the_same_tick(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record("chat-a", chat_id=1001))
    store.append_telegram_outbox(_record("chat-b", chat_id=2002))

    class _PerChatPort(TelegramFake):
        def send_message(self, *, chat_id: int, text: str, reply_to_message_id: int | None = None):  # type: ignore[no-untyped-def]
            if chat_id == 1001:
                raise TelegramError(TELEGRAM_UPSTREAM_ERROR, "scripted chat failure")
            return super().send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )

    port = _PerChatPort()
    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
    )

    assert dispatcher.dispatch_once() == 1

    failed = store.get_telegram_outbox("chat-a")
    sent = store.get_telegram_outbox("chat-b")
    assert failed is not None and sent is not None
    # Only the failing chat is requeued and charged an attempt.
    assert (failed.chat_id, failed.state, failed.attempts) == (1001, "queued", 1)
    assert (sent.chat_id, sent.state, sent.message_id) == (2002, "sent", 501)
    assert [call["chat_id"] for call in port.sent] == [2002]


def test_per_chat_last_error_code_comes_from_the_newest_failed_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(
        _record(
            "older-failed",
            chat_id=1001,
            state="failed",
            created_at="2026-11-17T08:00:00.000Z",
        )
    )
    store.append_telegram_outbox(
        _record(
            "newer-failed",
            chat_id=1001,
            state="failed",
            created_at="2026-11-17T09:00:00.000Z",
        )
    )
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute(
            "UPDATE telegram_outbox SET last_error_code = ? WHERE event_key = ?",
            ("ZZZ_OLDER", "older-failed"),
        )
        connection.execute(
            "UPDATE telegram_outbox SET last_error_code = ? WHERE event_key = ?",
            ("AAA_NEWEST", "newer-failed"),
        )
        connection.commit()

    statuses = {status.chat_id: status for status in store.telegram_chat_statuses()}

    # Lexicographic MAX would pick ZZZ_OLDER; the newest row's code wins.
    assert statuses[1001].last_error_code == "AAA_NEWEST"


def test_a_duplicate_at_the_cap_does_not_mark_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 1)
    store = _store(tmp_path)
    assert store.append_telegram_outbox(_record("same")) is True

    assert store.append_telegram_outbox(_record("same")) is False

    # Nothing was rejected: the duplicate is a silent no-op, not an overflow.
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) is None
    assert store.count_telegram_receipts() == 1


def test_the_overflow_marker_clears_after_the_queue_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 1)
    store = _store(tmp_path)
    assert store.append_telegram_outbox(_record("first")) is True
    assert store.append_telegram_outbox(_record("second")) is False
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) == TELEGRAM_QUEUE_FULL

    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: TelegramFake(),
        jitter=lambda: 0.0,
    )
    assert dispatcher.dispatch_once() == 1

    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) is None


def test_a_sustained_global_burst_never_grows_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 5)
    store = _store(tmp_path)

    accepted = sum(
        store.append_telegram_outbox(_record(f"msg-{index}", chat_id=1000 + index))
        for index in range(9)
    )

    assert accepted == 5
    assert store.count_telegram_receipts() == 5
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) == TELEGRAM_QUEUE_FULL


def test_a_sustained_per_chat_burst_never_grows_past_the_chat_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING", 1000)
    monkeypatch.setattr(store_module, "TELEGRAM_OUTBOX_MAX_PENDING_PER_CHAT", 3)
    store = _store(tmp_path)

    accepted = sum(
        store.append_telegram_outbox(_record(f"msg-{index}", chat_id=1001)) for index in range(7)
    )

    assert accepted == 3
    assert store.count_telegram_receipts() == 3
    assert store.get_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY) == TELEGRAM_QUEUE_FULL


def test_concurrent_enqueues_for_one_chat_claim_in_insertion_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    barrier = threading.Barrier(2)

    def _worker(prefix: str) -> None:
        barrier.wait(timeout=5)
        for index in range(4):
            store.append_telegram_outbox(_record(f"{prefix}-{index}", chat_id=1001))

    workers = [
        threading.Thread(target=_worker, args=("a",)),
        threading.Thread(target=_worker, args=("b",)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.is_alive() is False

    with store._connect() as connection:  # noqa: SLF001 - test setup
        enqueue_order = [
            str(row["event_key"])
            for row in connection.execute("SELECT event_key FROM telegram_outbox ORDER BY rowid")
        ]
    assert len(enqueue_order) == 8

    claimed = store.claim_due_telegram_outbox(now=NOW_TEXT, limit=100)

    # The claim's per-chat ordering matches the order the rows were enqueued.
    assert [row.event_key for row in claimed] == enqueue_order


def test_the_per_chat_claim_index_exists_and_is_reapplied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store._connect() as connection:  # noqa: SLF001 - test setup
        names = {row["name"] for row in connection.execute("PRAGMA index_list(telegram_outbox)")}
    assert "telegram_outbox_chat_due" in names

    # Simulate a database stamped at v30 before the index existed.
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute("DROP INDEX telegram_outbox_chat_due")
        connection.commit()

    store.initialize()

    with store._connect() as connection:  # noqa: SLF001 - test setup
        names = {row["name"] for row in connection.execute("PRAGMA index_list(telegram_outbox)")}
    assert "telegram_outbox_chat_due" in names


class _RejectingStore:
    def append_telegram_outbox(self, record: TelegramOutboxRecord) -> bool:
        return False


class _Secrets:
    def status_for(self, provider: str, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            configured=True,
            masked=None,
            last4=None,
            key_source="store",
            updated_at=None,
        )


class _Port:
    def get_me(self) -> object:
        return object()


class _AllowlistSettings:
    def read_telegram_allowlist(self) -> TelegramAllowlistSettings:
        return TelegramAllowlistSettings(
            chat_ids=(1001,),
            allowed_user_ids=(2002,),
            updated_at=None,
            updated_by=None,
        )


def _rejecting_service() -> TelegramService:
    return TelegramService(
        settings=Settings(env="test", data_dir="/tmp/oft-rejecting-outbox"),
        store=_RejectingStore(),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        market_data=None,  # type: ignore[arg-type]
        secrets=_Secrets(),  # type: ignore[arg-type]
        settings_service=_AllowlistSettings(),  # type: ignore[arg-type]
        port=_Port(),  # type: ignore[arg-type]
    )


def test_an_inbound_reply_is_not_fabricated_when_the_outbox_rejects_it() -> None:
    service = _rejecting_service()

    record = service.handle_update(
        TelegramUpdate(
            update_id=1,
            chat_id=1001,
            user_id=2002,
            text="/help",
            message_id=2,
        )
    )

    # The append was rejected (duplicate or full): never hand back a phantom
    # QUEUED receipt the caller could believe was persisted.
    assert record is None


def test_a_test_enqueue_raises_a_bounded_error_when_the_outbox_rejects_it() -> None:
    service = _rejecting_service()

    with pytest.raises(TelegramError) as raised:
        service.enqueue_test(actor="test")

    assert raised.value.code == TELEGRAM_UPSTREAM_ERROR
    assert "outbox is full" in raised.value.message
