"""Integration: the rate limiter composes a log alert and a Telegram alert safely."""

from __future__ import annotations

import ast
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from structlog.testing import capture_logs

from oft.application.rate_limiter import (
    COOLDOWN_429_SECONDS,
    RATE_LIMIT_COOLDOWN_STATE_KEY,
    CompositeAlertSink,
    GlobalRateLimiter,
    StructlogAlertSink,
    TelegramAlertSink,
)
from oft.clock import FixedClock

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


class _RecordingNotifications:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[Any] = []
        self.fail = fail

    def enqueue_event(self, event: Any) -> object | None:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.events.append(event)
        return object()


def _sink(notifications: _RecordingNotifications) -> TelegramAlertSink:
    return TelegramAlertSink(notifications, clock=FixedClock(NOW))


def test_429_alerts_both_the_log_and_telegram_once() -> None:
    notifications = _RecordingNotifications()
    limiter = GlobalRateLimiter(
        clock=FixedClock(NOW),
        alert_sink=CompositeAlertSink(StructlogAlertSink(), _sink(notifications)),
    )

    with capture_logs() as logs:
        limiter.note_status(429, endpoint="/fapi/v1/aggTrades")
        # Repeated provider signals inside the same cooldown are a no-op.
        limiter.note_status(429, endpoint="/fapi/v1/aggTrades")
        limiter.note_status(418, endpoint="/fapi/v1/aggTrades")

    assert limiter.in_cooldown()
    assert len(notifications.events) == 1
    event = notifications.events[0]
    assert event.event_type == "degradation"
    assert event.payload["state"] == "DEGRADED"
    assert event.payload["reason_code"] == "RATE_LIMITED_429"
    assert event.payload["subject"] == "provider:binance"
    # The existing structlog alert still fires.
    rate_logs = [entry for entry in logs if entry["event"] == "binance.rate_limit"]
    assert len(rate_logs) == 1
    assert rate_logs[0]["status"] == 429


def test_cooldown_expiry_emits_one_recovery_alert() -> None:
    class _Clock:
        def __init__(self) -> None:
            self.instant = NOW

        def now(self) -> datetime:
            return self.instant

    clock = _Clock()
    notifications = _RecordingNotifications()
    limiter = GlobalRateLimiter(clock=clock, alert_sink=_sink(notifications))
    limiter.note_status(429, endpoint="/fapi/v1/aggTrades")

    clock.instant = NOW + timedelta(seconds=COOLDOWN_429_SECONDS + 1)
    assert limiter.in_cooldown() is False
    # A second check is a no-op: recovery is emitted once per episode.
    assert limiter.in_cooldown() is False

    states = [event.payload["state"] for event in notifications.events]
    assert states == ["DEGRADED", "RECOVERED"]
    assert notifications.events[1].payload["reason_code"] == "PROVIDER_RECOVERED"


def test_a_telegram_failure_is_recorded_and_never_blocks_the_limiter() -> None:
    notifications = _RecordingNotifications(fail=True)
    telegram_sink = _sink(notifications)
    limiter = GlobalRateLimiter(
        clock=FixedClock(NOW),
        alert_sink=CompositeAlertSink(StructlogAlertSink(), telegram_sink),
    )

    with capture_logs() as logs:
        decision = limiter.note_status(429, endpoint="/fapi/v1/aggTrades")

    assert decision is not None
    assert decision.reason == "COOLDOWN"
    assert limiter.in_cooldown() is True
    assert telegram_sink.failures == 1
    assert telegram_sink.last_error_code == "RuntimeError"
    assert any(entry["event"] == "binance.rate_limit" for entry in logs)


def test_a_failing_sink_does_not_stop_other_sinks() -> None:
    notifications = _RecordingNotifications()

    class _BrokenSink:
        def alert(self, event: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("broken")

    sink = CompositeAlertSink(_BrokenSink(), _sink(notifications))

    with capture_logs():
        sink.alert(
            "binance.rate_limit",
            {
                "status": 429,
                "cooldown_seconds": 60,
                "endpoint": "/x",
                "until": "2026-11-17T09:31:00.000Z",
            },
        )

    assert len(notifications.events) == 1


class _StateStore:
    """The durable ``system_state`` slice the limiter persists its epoch in."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def get_system_state(self, key: str) -> str | None:
        return self.state.get(key)

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        self.state[key] = value


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def test_a_slow_alert_sink_does_not_block_request_acquisition() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _SlowSink:
        def alert(self, event: str, payload: dict[str, Any]) -> None:
            entered.set()
            assert release.wait(5)

    limiter = GlobalRateLimiter(clock=FixedClock(NOW), alert_sink=_SlowSink())
    worker = threading.Thread(
        target=lambda: limiter.note_status(429, endpoint="/fapi/v1/aggTrades")
    )
    worker.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        decision = limiter.acquire("/fapi/v1/aggTrades")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        worker.join(timeout=2)

    assert decision.reason == "COOLDOWN"
    assert elapsed < 1.0


def test_the_cooldown_epoch_survives_a_restart() -> None:
    store = _StateStore()
    clock = _MutableClock(NOW)
    first = GlobalRateLimiter(clock=clock, store=store)  # type: ignore[arg-type]
    first.note_status(429, endpoint="/fapi/v1/aggTrades")
    assert store.state["telegram.rate_limit.cooldown_until"]

    notifications = _RecordingNotifications()
    restarted = GlobalRateLimiter(
        clock=clock,
        alert_sink=_sink(notifications),
        store=store,  # type: ignore[arg-type]
    )
    # Still cooling: a restart must not re-emit DEGRADED.
    assert restarted.in_cooldown() is True
    restarted.note_status(429, endpoint="/fapi/v1/aggTrades")
    assert notifications.events == []

    clock.instant = NOW + timedelta(seconds=COOLDOWN_429_SECONDS + 1)
    assert restarted.in_cooldown() is False

    assert [event.payload["state"] for event in notifications.events] == ["RECOVERED"]
    assert store.state["telegram.rate_limit.cooldown_until"] == ""


class _BlockingClearStore(_StateStore):
    """A store that holds the recovery clear open for a deterministic race."""

    def __init__(self) -> None:
        super().__init__()
        self.clear_started = threading.Event()
        self.allow_clear = threading.Event()
        self.writes: list[str] = []

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        self.writes.append(value)
        if value == "":
            self.clear_started.set()
            assert self.allow_clear.wait(5)
        super().set_system_state(key, value, updated_at=updated_at)


def test_a_new_epoch_is_never_blanked_by_a_concurrent_recovery_clear() -> None:
    store = _BlockingClearStore()
    clock = _MutableClock(NOW)
    notifications = _RecordingNotifications()
    limiter = GlobalRateLimiter(
        clock=clock,
        alert_sink=_sink(notifications),
        store=store,  # type: ignore[arg-type]
    )

    limiter.note_status(429, endpoint="/fapi/v1/aggTrades")
    first_epoch = store.state[RATE_LIMIT_COOLDOWN_STATE_KEY]
    assert first_epoch
    clock.instant = NOW + timedelta(seconds=COOLDOWN_429_SECONDS + 1)

    # The recovery path is mid-clear, holding the state lock while the store
    # write blocks. A concurrent 429 must not be able to persist a new epoch
    # that this clear then blanks.
    recovery = threading.Thread(target=limiter.in_cooldown)
    recovery.start()
    assert store.clear_started.wait(2)

    late: list[object] = []
    second = threading.Thread(
        target=lambda: late.append(limiter.note_status(429, endpoint="/fapi/v1/aggTrades"))
    )
    second.start()
    assert store.writes == [first_epoch, ""]

    store.allow_clear.set()
    recovery.join(timeout=5)
    second.join(timeout=5)

    assert not recovery.is_alive() and not second.is_alive()
    assert late and late[0] is not None
    new_epoch = store.state[RATE_LIMIT_COOLDOWN_STATE_KEY]
    assert new_epoch and new_epoch != ""
    # The clear was serialized under the lock, so the new epoch landed last.
    assert store.writes == [first_epoch, "", new_epoch]
    assert [event.payload["state"] for event in notifications.events] == [
        "DEGRADED",
        "RECOVERED",
        "DEGRADED",
    ]


def test_recovered_never_overtakes_the_same_episodes_degraded() -> None:
    degraded_entered = threading.Event()
    release_degraded = threading.Event()
    emitted: list[str] = []

    class _BlockingDegradedSink:
        def alert(self, event: str, payload: dict[str, Any]) -> None:
            emitted.append(event)
            if event == "binance.rate_limit":
                degraded_entered.set()
                assert release_degraded.wait(5)

    clock = _MutableClock(NOW)
    limiter = GlobalRateLimiter(clock=clock, alert_sink=_BlockingDegradedSink())
    degraded = threading.Thread(
        target=lambda: limiter.note_status(429, endpoint="/fapi/v1/aggTrades")
    )
    degraded.start()
    assert degraded_entered.wait(2)

    # Advance past the cooldown while the DEGRADED push is still in the sink;
    # the recovery must park behind it instead of emitting RECOVERED first.
    clock.instant = NOW + timedelta(seconds=COOLDOWN_429_SECONDS + 1)
    recovered = threading.Thread(target=limiter.in_cooldown)
    recovered.start()
    deadline = time.monotonic() + 2
    while limiter.cooldown_until is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert limiter.cooldown_until is None
    assert emitted == ["binance.rate_limit"]
    assert recovered.is_alive()

    release_degraded.set()
    degraded.join(timeout=5)
    recovered.join(timeout=5)

    assert not degraded.is_alive() and not recovered.is_alive()
    assert emitted == ["binance.rate_limit", "binance.rate_limit_recovered"]


def test_no_alert_path_imports_an_exchange_or_order_module() -> None:
    alert_files = [
        "src/oft/application/telegram_events.py",
        "src/oft/application/telegram_messages.py",
        "src/oft/application/notifications.py",
        "src/oft/application/telegram_watchdog.py",
        "src/oft/application/rate_limiter.py",
    ]
    forbidden = (
        "oft.adapters.binance",
        "oft.application.paper_broker",
        "oft.application.execution_gateway",
    )
    for relative in alert_files:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for name in imported
            for prefix in forbidden
        ), relative
