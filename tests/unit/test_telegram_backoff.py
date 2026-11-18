"""Retry timing: bounded exponential backoff, injected jitter, 429 floors."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from fakes.telegram import ScriptedBotApiTransport
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
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
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


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


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class _TimeoutTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


def _dispatcher_for(store: SqliteStore, port: object, *, clock: _MutableClock) -> OutboxDispatcher:
    return OutboxDispatcher(
        store=store,
        clock=clock,
        port_resolver=lambda: port,  # type: ignore[arg-type,return-value]
        jitter=lambda: 0.0,
    )


@pytest.mark.parametrize(
    ("name", "http_status", "retry_after", "expected_code", "expected_attempts", "next_at"),
    [
        ("token_invalid", 401, None, "TELEGRAM_TOKEN_INVALID", 1, "2026-11-17T09:30:02.000Z"),
        ("conflict", 409, None, "TELEGRAM_CONFLICT", 1, "2026-11-17T09:30:02.000Z"),
        ("server_error", 500, None, "TELEGRAM_UPSTREAM_ERROR", 1, "2026-11-17T09:30:02.000Z"),
        ("bad_gateway", 502, None, "TELEGRAM_UPSTREAM_ERROR", 1, "2026-11-17T09:30:02.000Z"),
        ("rate_limited", 429, 30, "TELEGRAM_RATE_LIMITED", 0, "2026-11-17T09:30:30.000Z"),
        ("rate_limited_zero", 429, 0, "TELEGRAM_RATE_LIMITED", 0, "2026-11-17T09:30:02.000Z"),
    ],
)
def test_every_bot_api_failure_has_an_exact_injected_clock_next_attempt(
    tmp_path,
    name: str,
    http_status: int,
    retry_after: int | None,
    expected_code: str,
    expected_attempts: int,
    next_at: str,
) -> None:
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
    transport = ScriptedBotApiTransport()
    transport.script_error("sendMessage", http_status, retry_after=retry_after)
    adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)

    assert _dispatcher_for(store, adapter, clock=_MutableClock(NOW)).dispatch_once() == 0

    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert receipt.last_error_code == expected_code, name
    assert receipt.attempts == expected_attempts, name
    assert receipt.next_attempt_at == next_at, name


def test_a_timeout_is_a_bounded_upstream_failure_with_an_exact_next_attempt(tmp_path) -> None:
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
    adapter = TelegramBotApiAdapter(
        token_resolver=lambda: SENTINEL,
        transport=_TimeoutTransport(),
    )

    assert _dispatcher_for(store, adapter, clock=_MutableClock(NOW)).dispatch_once() == 0

    receipt = store.get_telegram_outbox("update:1:reply")
    assert receipt is not None
    assert (receipt.state, receipt.attempts, receipt.last_error_code) == (
        "queued",
        1,
        "TELEGRAM_UPSTREAM_ERROR",
    )
    assert receipt.next_attempt_at == "2026-11-17T09:30:02.000Z"


def test_dispatch_never_calls_wall_clock_sleep(tmp_path, monkeypatch) -> None:
    import time

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

    def _no_sleep(_seconds: float) -> None:
        raise AssertionError("dispatch must use the injected clock, never sleep()")

    monkeypatch.setattr(time, "sleep", _no_sleep)

    assert _dispatcher_for(store, TelegramFake(), clock=_MutableClock(NOW)).dispatch_once() == 1
