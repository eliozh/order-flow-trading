"""Outbox idempotency, the receipt state machine, and bounded dispatch retries."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from fakes.telegram import ScriptedBotApiTransport
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.adapters.telegram_fake import TelegramFake
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _record(event_key: str = "update:1:reply", *, text: str = "STATUS") -> TelegramOutboxRecord:
    return TelegramOutboxRecord(
        event_key=event_key,
        kind="query_reply",
        chat_id=1001,
        reply_to_message_id=101,
        payload_json=json.dumps({"text": text}),
        priority=0,
        state="queued",
        attempts=0,
        next_attempt_at=NOW_TEXT,
        message_id=None,
        last_error_code=None,
        created_at=NOW_TEXT,
        sent_at=None,
    )


def test_duplicate_append_is_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.append_telegram_outbox(_record()) is True
    assert store.append_telegram_outbox(_record()) is False
    assert len(store.list_telegram_receipts()) == 1


def test_dispatch_sends_once_and_records_the_telegram_message_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake(sent_message_id_start=700)
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(_record())

    sent = dispatcher.dispatch_once()

    assert sent == 1
    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert receipt.state == "sent"
    assert receipt.message_id == 701
    assert receipt.sent_at == NOW_TEXT
    assert receipt.attempts == 0
    assert port.sent[0]["chat_id"] == 1001
    assert port.sent[0]["reply_to_message_id"] == 101
    assert port.sent[0]["text"] == "STATUS"
    assert dispatcher.dispatch_once() == 0
    assert len(port.sent) == 1


def test_send_message_never_uses_parse_mode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport = ScriptedBotApiTransport()
    adapter = TelegramBotApiAdapter(
        token_resolver=lambda: SENTINEL,
        transport=transport,
    )
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: adapter)
    store.append_telegram_outbox(_record(text="STATUS\nenv: test"))

    assert dispatcher.dispatch_once() == 1
    assert transport.sent
    assert "parse_mode" not in transport.sent[0]["json"]
    # The outbound body carries the rendered reply only, never the token.
    assert SENTINEL not in transport.sent[0]["text"]
    assert SENTINEL not in json.dumps(transport.sent[0]["json"])
    assert store.get_telegram_outbox("update:1:reply").state == "sent"  # type: ignore[union-attr]


def test_a_transient_failure_is_retried_then_sent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake(fail_send_times=2)
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(_record())

    assert dispatcher.dispatch_once() == 0
    first = store.get_telegram_outbox("update:1:reply")
    assert first is not None
    assert (first.state, first.attempts, first.last_error_code) == (
        "queued",
        1,
        "TELEGRAM_UPSTREAM_ERROR",
    )

    assert dispatcher.dispatch_once() == 0
    second = store.get_telegram_outbox("update:1:reply")
    assert second is not None
    assert (second.state, second.attempts) == ("queued", 2)

    assert dispatcher.dispatch_once() == 1
    final = store.get_telegram_outbox("update:1:reply")
    assert final is not None
    assert (final.state, final.message_id) == ("sent", 501)
    # A successful retry clears the transient failure code.
    assert final.last_error_code is None


def test_mark_sent_only_transitions_a_row_in_the_sending_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record())

    assert store.mark_telegram_sent("update:1:reply", message_id=9, sent_at=NOW_TEXT) is False
    queued = store.get_telegram_outbox("update:1:reply")
    assert queued is not None
    assert queued.state == "queued"
    assert queued.last_error_code is None


def test_mark_attempt_failed_only_transitions_a_sending_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(_record())

    assert (
        store.mark_telegram_attempt_failed(
            "update:1:reply",
            error_code="TELEGRAM_UPSTREAM_ERROR",
            next_attempt_at=NOW_TEXT,
            final=False,
        )
        is False
    )
    queued = store.get_telegram_outbox("update:1:reply")
    assert queued is not None
    assert (queued.state, queued.attempts, queued.last_error_code) == ("queued", 0, None)


def test_retries_are_bounded_at_three_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake(fail_send_times=10)
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(_record())

    for _ in range(3):
        assert dispatcher.dispatch_once() == 0
    failed = store.get_telegram_outbox("update:1:reply")
    assert failed is not None
    assert (failed.state, failed.attempts, failed.last_error_code) == (
        "failed",
        3,
        "TELEGRAM_UPSTREAM_ERROR",
    )
    assert dispatcher.dispatch_once() == 0
    assert len(port.send_calls) == 3


def test_a_malformed_payload_is_failed_without_a_send(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake()
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(_record())
    # Corrupt the row directly so the dispatcher must fail closed, not crash.
    with store._connect() as connection:  # noqa: SLF001 - test setup
        connection.execute(
            "UPDATE telegram_outbox SET payload_json = 'not-json' WHERE event_key = ?",
            ("update:1:reply",),
        )
        connection.commit()

    assert dispatcher.dispatch_once() == 0
    failed = store.get_telegram_outbox("update:1:reply")
    assert failed is not None
    assert failed.state == "failed"
    assert failed.last_error_code == "TELEGRAM_DELIVERY_FAILED"
    assert port.send_calls == []


def test_an_overlapping_dispatch_tick_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake(gate=threading.Event())
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(_record())
    results: list[int] = []

    worker = threading.Thread(target=lambda: results.append(dispatcher.dispatch_once()))
    worker.start()
    assert port.entered.wait(timeout=5)
    assert dispatcher.dispatch_once() == 0
    port.gate.set()  # type: ignore[union-attr]
    worker.join(timeout=5)
    assert results == [1]


def test_claim_orders_by_priority_and_is_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_telegram_outbox(
        TelegramOutboxRecord(**{**_record("update:1:reply").__dict__, "priority": -1})
    )
    store.append_telegram_outbox(
        TelegramOutboxRecord(**{**_record("update:2:reply").__dict__, "priority": 5})
    )

    claimed = store.claim_due_telegram_outbox(now="2026-11-17T09:30:00.000Z", limit=1)

    assert [item.event_key for item in claimed] == ["update:2:reply"]
    assert claimed[0].state == "sending"


def test_an_unexpected_send_failure_is_retried_and_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _Exploding(TelegramFake):
        def send_message(self, *, chat_id: int, text: str, reply_to_message_id: int | None = None):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"boom for chat {chat_id}")

    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=_Exploding)
    store.append_telegram_outbox(_record())

    assert dispatcher.dispatch_once() == 0
    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert (receipt.state, receipt.attempts, receipt.last_error_code) == (
        "queued",
        1,
        "TELEGRAM_UPSTREAM_ERROR",
    )


def test_a_payload_without_text_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    port = TelegramFake()
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: port)
    store.append_telegram_outbox(
        TelegramOutboxRecord(**{**_record().__dict__, "payload_json": '{"note": "x"}'})
    )

    assert dispatcher.dispatch_once() == 0
    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert (receipt.state, receipt.last_error_code) == ("failed", "TELEGRAM_DELIVERY_FAILED")
    assert port.send_calls == []


def test_an_outer_store_failure_does_not_escape_the_tick(tmp_path: Path) -> None:
    class _BrokenStore:
        def claim_due_telegram_outbox(self, *, now: str, limit: int = 10):  # type: ignore[no-untyped-def]
            raise RuntimeError("store down")

    dispatcher = OutboxDispatcher(
        store=_BrokenStore(),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        port_resolver=TelegramFake,
    )

    assert dispatcher.dispatch_once() == 0


def test_the_scheduler_wrapper_swallows_failures() -> None:
    from oft.application.telegram_dispatch import dispatch_guard

    class _BrokenDispatcher:
        def dispatch_once(self) -> int:
            raise RuntimeError("tick failed")

    dispatch_guard(_BrokenDispatcher()).__call__()  # type: ignore[arg-type]


def test_the_scheduler_rejects_a_non_positive_interval(tmp_path: Path) -> None:
    import pytest

    from oft.application.telegram_dispatch import TelegramDispatchScheduler

    with pytest.raises(ValueError):
        TelegramDispatchScheduler(
            dispatcher=OutboxDispatcher(
                store=_store(tmp_path),
                clock=FixedClock(NOW),
                port_resolver=TelegramFake,
            ),
            interval_seconds=0,
        )
