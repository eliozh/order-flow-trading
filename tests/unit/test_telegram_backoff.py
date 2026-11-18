"""Retry timing: bounded exponential backoff, injected jitter, 429 floors."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.telegram_dispatch import (
    OutboxDispatcher,
    backoff_delay_seconds,
    retry_after_delay_seconds,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS,
    TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS,
    TELEGRAM_RETRY_AFTER_MAX_SECONDS,
)
from oft.ports.store import TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"


def test_exponential_backoff_is_increasing_and_bounded() -> None:
    delays = [backoff_delay_seconds(attempt, jitter_fraction=0.0) for attempt in range(6)]

    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    assert all(delay <= TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS for delay in delays)
    assert backoff_delay_seconds(50, jitter_fraction=0.0) == float(
        TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS
    )


def test_injected_jitter_stays_within_one_base_and_never_shrinks() -> None:
    base = backoff_delay_seconds(1, jitter_fraction=0.0)

    for fraction in (0.0, 0.1, 0.5, 0.99, 1.0):
        delay = backoff_delay_seconds(1, jitter_fraction=fraction)
        assert base <= delay <= min(2 * base, TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS)


def test_jitter_is_clamped_to_the_unit_interval() -> None:
    minimum = backoff_delay_seconds(0, jitter_fraction=0.0)
    maximum = backoff_delay_seconds(0, jitter_fraction=1.0)

    assert backoff_delay_seconds(0, jitter_fraction=-5.0) == minimum
    assert backoff_delay_seconds(0, jitter_fraction=5.0) == maximum
    assert backoff_delay_seconds(0, jitter_fraction=float("nan")) == minimum


def test_retry_after_is_a_floor_and_is_capped() -> None:
    assert retry_after_delay_seconds(30) == 30
    assert retry_after_delay_seconds(0) == TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS
    assert retry_after_delay_seconds(10_000) == TELEGRAM_RETRY_AFTER_MAX_SECONDS


def test_dispatcher_uses_the_injected_jitter_source(tmp_path) -> None:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    store.append_telegram_outbox(
        TelegramOutboxRecord(
            event_key="update:1:reply",
            kind="query_reply",
            chat_id=1001,
            reply_to_message_id=None,
            payload_json=json.dumps({"text": "STATUS"}),
            priority=0,
            state="queued",
            attempts=0,
            next_attempt_at=NOW_TEXT,
            message_id=None,
            last_error_code=None,
            created_at=NOW_TEXT,
            sent_at=None,
        )
    )
    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: TelegramFake(fail_send_times=1),
        jitter=lambda: 0.5,
    )

    assert dispatcher.dispatch_once() == 0

    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    # base 2 s + 50 % jitter = 3 s, proving the injected source was consulted.
    assert receipt.next_attempt_at == "2026-11-17T09:30:03.000Z"
