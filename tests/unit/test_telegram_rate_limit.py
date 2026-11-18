"""Per-chat token bucket: bursts defer locally without consuming attempts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.telegram_dispatch import ChatTokenBucket, OutboxDispatcher
from oft.config import Settings
from oft.domain.telegram import TELEGRAM_LOCAL_RATE_LIMITED
from oft.ports.store import TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant = self.instant + timedelta(seconds=seconds)


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _record(event_key: str) -> TelegramOutboxRecord:
    return TelegramOutboxRecord(
        event_key=event_key,
        kind="query_reply",
        chat_id=1001,
        reply_to_message_id=None,
        payload_json=json.dumps({"text": event_key}),
        priority=0,
        state="queued",
        attempts=0,
        next_attempt_at=NOW_TEXT,
        message_id=None,
        last_error_code=None,
        created_at=NOW_TEXT,
        sent_at=None,
    )


def test_a_burst_drains_the_bucket_then_defers_to_the_refill_instant() -> None:
    bucket = ChatTokenBucket(capacity=2, refill_per_second=1.0)

    assert bucket.consume(1001, NOW) is None
    assert bucket.consume(1001, NOW) is None

    deferred = bucket.consume(1001, NOW)

    assert deferred == NOW + timedelta(seconds=1)


def test_chats_have_independent_buckets() -> None:
    bucket = ChatTokenBucket(capacity=1, refill_per_second=1.0)

    assert bucket.consume(1001, NOW) is None
    assert bucket.consume(1001, NOW) is not None
    # A drained chat cannot starve another chat's burst.
    assert bucket.consume(2002, NOW) is None


def test_refill_is_continuous_and_capped_at_capacity() -> None:
    bucket = ChatTokenBucket(capacity=1, refill_per_second=1.0)
    assert bucket.consume(1001, NOW) is None

    partial = bucket.consume(1001, NOW + timedelta(seconds=0.5))
    assert partial == NOW + timedelta(seconds=1)

    assert bucket.consume(1001, NOW + timedelta(seconds=1)) is None
    # Idle time never accumulates more than the capacity (one token here).
    assert bucket.consume(1001, NOW + timedelta(seconds=100)) is None
    assert bucket.consume(1001, NOW + timedelta(seconds=100)) is not None


@pytest.mark.parametrize(
    ("capacity", "refill"),
    [(0, 1.0), (-1, 1.0), (1.0, 0), (1.0, -2.0)],
)
def test_invalid_bucket_parameters_are_rejected(capacity: float, refill: float) -> None:
    with pytest.raises(ValueError):
        ChatTokenBucket(capacity=capacity, refill_per_second=refill)


def test_a_deferred_row_is_requeued_without_consuming_an_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = _MutableClock(NOW)
    port = TelegramFake(sent_message_id_start=700)
    dispatcher = OutboxDispatcher(
        store=store,
        clock=clock,
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
        chat_burst=1.0,
        chat_refill_per_second=1.0,
    )
    store.append_telegram_outbox(_record("update:1:reply"))
    store.append_telegram_outbox(_record("update:2:reply"))

    # The first message drains the single-token burst.
    assert dispatcher.dispatch_once() == 1
    assert len(port.send_calls) == 1

    # The next message to the same chat is deferred, not sent or failed.
    assert dispatcher.dispatch_once() == 0
    deferred = store.get_telegram_outbox("update:2:reply")
    assert deferred is not None
    assert deferred.state == "queued"
    assert deferred.attempts == 0
    assert deferred.last_error_code == TELEGRAM_LOCAL_RATE_LIMITED
    assert deferred.next_attempt_at == "2026-11-17T09:30:01.000Z"

    # Still inside the deferral: no send.
    assert dispatcher.dispatch_once() == 0
    assert len(port.send_calls) == 1

    clock.advance(1)
    assert dispatcher.dispatch_once() == 1
    sent = store.get_telegram_outbox("update:2:reply")
    assert sent is not None
    assert (sent.state, sent.attempts, sent.last_error_code) == ("sent", 0, None)
