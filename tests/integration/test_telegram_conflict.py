"""Poller conflict/token handling, backoff, stop responsiveness, and isolation.

The last two tests prove Telegram I/O (a blocked long poll or a slow send) runs
on its own thread and cannot delay an independent APScheduler tick.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from fakes.telegram import ScriptedBotApiTransport
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.adapters.telegram_fake import TelegramFake
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_poller import TelegramPoller
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    POLLER_HEARTBEAT_KEY,
    POLLER_LAST_ERROR_KEY,
    POLLER_STATE_CONFLICT,
    POLLER_STATE_DEGRADED,
    POLLER_STATE_KEY,
)
from oft.ports.telegram import (
    TELEGRAM_CONFLICT,
    TELEGRAM_TOKEN_INVALID,
    TELEGRAM_UPSTREAM_ERROR,
    TelegramError,
    TelegramUpdatePage,
)

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


@pytest.fixture(autouse=True)
def _hermetic_secret_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the fail keyring + a per-test master key so CI stays hermetic."""
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("OFT_MASTER_KEY", "per-test-master-key-0123456789abcdef")
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


class _FailingPort:
    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        self._code = code
        self._retry_after = retry_after

    def get_me(self) -> Any:
        raise TelegramError(self._code, "scripted failure", retry_after=self._retry_after)

    def get_updates(self, *, offset: int | None = None, timeout_seconds: int = 0) -> Any:
        raise TelegramError(self._code, "scripted failure", retry_after=self._retry_after)

    def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> Any:
        raise TelegramError(self._code, "scripted failure", retry_after=self._retry_after)


def _poller(
    store: SqliteStore, resolver: Callable[[], Any], *, interval: float = 1.0
) -> TelegramPoller:
    return TelegramPoller(
        service=cast(Any, None),
        store=store,
        clock=FixedClock(NOW),
        port_resolver=resolver,
        long_poll_timeout=0,
        poll_interval_seconds=interval,
    )


def test_the_adapter_maps_401_and_404_to_an_invalid_token() -> None:
    for code in (401, 404):
        transport = ScriptedBotApiTransport()
        transport.script_error("getUpdates", code)
        adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)

        with pytest.raises(TelegramError) as raised:
            adapter.get_updates(offset=None)

        assert raised.value.code == TELEGRAM_TOKEN_INVALID


def test_a_429_is_mapped_to_the_rate_limit_code() -> None:
    transport = ScriptedBotApiTransport()
    transport.script_error("getUpdates", 429, retry_after=30)
    adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)

    with pytest.raises(TelegramError) as raised:
        adapter.get_updates(offset=None)

    assert raised.value.code == "TELEGRAM_RATE_LIMITED"
    assert raised.value.retry_after == 30


def test_a_404_poll_marks_token_invalid_and_backs_off_without_a_heartbeat(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transport = ScriptedBotApiTransport()
    transport.script_error("getUpdates", 404)
    transport.script_error("getUpdates", 404)
    adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)
    poller = _poller(store, lambda: adapter)

    assert poller.poll_once() == 0
    assert poller.poll_once() == 0

    # The state key carries the enum only; the raw port code is the reason.
    assert store.get_system_state(POLLER_STATE_KEY) == POLLER_STATE_DEGRADED
    assert store.get_system_state(POLLER_LAST_ERROR_KEY) == TELEGRAM_TOKEN_INVALID
    assert poller._wait_override == 30.0  # noqa: SLF001 - the configured state backoff
    # The heartbeat means "last successful poll", so a failure never writes it.
    assert store.get_system_state(POLLER_HEARTBEAT_KEY) is None


def test_a_429_poll_honours_the_retry_after_floor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    poller = _poller(
        store,
        lambda: _FailingPort("TELEGRAM_RATE_LIMITED", retry_after=120),
    )

    assert poller.poll_once() == 0

    # The transient backoff (1 s base, doubled once) is smaller than the
    # server-provided floor, so the poller must wait the full 120 s.
    assert poller._wait_override == 120.0  # noqa: SLF001 - the scheduled delay
    assert store.get_system_state(POLLER_STATE_KEY) == POLLER_STATE_DEGRADED
    assert store.get_system_state(POLLER_LAST_ERROR_KEY) == "TELEGRAM_RATE_LIMITED"
    assert store.get_system_state(POLLER_HEARTBEAT_KEY) is None


@pytest.mark.parametrize("retry_after", [0, 1])
def test_a_429_retry_after_below_the_transient_backoff_does_not_shorten_the_wait(
    tmp_path: Path, retry_after: int
) -> None:
    store = _store(tmp_path)
    poller = _poller(
        store,
        lambda: _FailingPort("TELEGRAM_RATE_LIMITED", retry_after=retry_after),
    )

    assert poller.poll_once() == 0

    # The first transient backoff is 2 s (1 s base, doubled once). A small or
    # zero server value must not pull the wait below that floor, or a 429 with
    # retry_after=0 would spin the poll loop.
    assert poller._wait_override == 2.0  # noqa: SLF001 - the scheduled delay


def test_a_429_retry_after_above_the_transient_backoff_raises_the_wait(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    poller = _poller(
        store,
        lambda: _FailingPort("TELEGRAM_RATE_LIMITED", retry_after=7),
    )

    assert poller.poll_once() == 0

    assert poller._wait_override == 7.0  # noqa: SLF001 - the server-provided floor


def test_a_409_marks_conflict_and_backs_off_without_a_tight_loop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    poller = _poller(store, lambda: _FailingPort(TELEGRAM_CONFLICT))

    assert poller.poll_once() == 0
    first_wait = poller._wait_override  # noqa: SLF001
    assert poller.poll_once() == 0
    second_wait = poller._wait_override  # noqa: SLF001

    assert store.get_system_state(POLLER_STATE_KEY) == POLLER_STATE_CONFLICT
    assert store.get_system_state(POLLER_LAST_ERROR_KEY) == TELEGRAM_CONFLICT
    assert first_wait is not None and first_wait >= 1.0
    assert second_wait is not None and second_wait > first_wait
    assert store.get_system_state(POLLER_HEARTBEAT_KEY) is None


def test_a_network_failure_marks_degraded_and_backs_off(tmp_path: Path) -> None:
    store = _store(tmp_path)
    poller = _poller(store, lambda: _FailingPort(TELEGRAM_UPSTREAM_ERROR))

    assert poller.poll_once() == 0
    first = poller._wait_override  # noqa: SLF001
    assert poller.poll_once() == 0
    second = poller._wait_override  # noqa: SLF001

    assert store.get_system_state(POLLER_STATE_KEY) == POLLER_STATE_DEGRADED
    assert store.get_system_state(POLLER_LAST_ERROR_KEY) == TELEGRAM_UPSTREAM_ERROR
    assert first is not None and second is not None and second > first
    assert store.get_system_state(POLLER_HEARTBEAT_KEY) is None


def test_a_successful_poll_resets_the_state_and_writes_the_heartbeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    poller = _poller(store, lambda: TelegramFake())

    assert poller.poll_once() == 0

    assert store.get_system_state(POLLER_STATE_KEY) == "RUNNING"
    assert store.get_system_state(POLLER_LAST_ERROR_KEY) == ""
    assert store.get_system_state(POLLER_HEARTBEAT_KEY) == NOW_TEXT


class _BlockingPollerPort:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_me(self) -> Any:
        raise AssertionError("not used")

    def get_updates(self, *, offset: int | None = None, timeout_seconds: int = 0) -> Any:
        self.entered.set()
        self.release.wait(timeout=10)
        return TelegramUpdatePage(updates=(), max_update_id=None)

    def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> Any:
        raise AssertionError("not used")


@contextmanager
def _independent_scheduler() -> Iterator[list[int]]:
    """A real trigger scheduler proving Telegram I/O cannot delay another tick."""
    from oft.application.scheduler import TriggerDaemonScheduler

    ticks: list[int] = []
    scheduler = TriggerDaemonScheduler(interval_seconds=1, job=lambda: ticks.append(1))
    scheduler.start()
    try:
        yield ticks
    finally:
        scheduler.shutdown(wait=False)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached before the timeout")


def test_a_blocked_long_poll_does_not_delay_an_independent_scheduler_tick(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    port = _BlockingPollerPort()
    poller = _poller(store, lambda: port, interval=0.01)
    poller.start()
    assert port.entered.wait(timeout=5)

    try:
        with _independent_scheduler() as ticks:
            _wait_until(lambda: len(ticks) >= 2)

        # A stop request is immediate even while the long poll is still blocked.
        started = time.monotonic()
        poller.shutdown(wait=False)
        assert time.monotonic() - started < 1.0
        assert poller._thread is not None and poller._thread.is_alive()  # noqa: SLF001
    finally:
        port.release.set()
        thread = poller._thread  # noqa: SLF001 - join the released worker
        if thread is not None:
            thread.join(timeout=5)

    assert poller.is_running is False


def test_a_slow_send_does_not_delay_an_independent_scheduler_tick(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gate = threading.Event()
    port = TelegramFake(gate=gate)
    dispatcher = OutboxDispatcher(
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        jitter=lambda: 0.0,
    )
    from oft.ports.store import TelegramOutboxRecord

    store.append_telegram_outbox(
        TelegramOutboxRecord(
            event_key="update:1:reply",
            kind="query_reply",
            chat_id=1001,
            reply_to_message_id=None,
            payload_json='{"text": "STATUS"}',
            state="queued",
            next_attempt_at=NOW_TEXT,
            created_at=NOW_TEXT,
        )
    )
    worker = threading.Thread(target=dispatcher.dispatch_once, daemon=True)
    worker.start()
    assert port.entered.wait(timeout=5)

    try:
        with _independent_scheduler() as ticks:
            _wait_until(lambda: len(ticks) >= 2)
    finally:
        gate.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
