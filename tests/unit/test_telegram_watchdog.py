"""The one-shot host-level watchdog: one alert and one recovery per outage."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oft.adapters.telegram_fake import TelegramFake
from oft.application.health import DaemonProbe
from oft.application.telegram_watchdog import (
    WATCHDOG_STATE_KEY,
    resolve_private_chat,
    run_watchdog_once,
)
from oft.clock import FixedClock

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
TOKEN_SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
CHAT_ID = 1001


class _Store:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def get_system_state(self, key: str) -> str | None:
        return self.state.get(key)

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        self.state[key] = value


def _run(
    store: _Store,
    *,
    up: bool,
    port: TelegramFake | None = None,
    chat_id: int | None = CHAT_ID,
) -> Any:
    return run_watchdog_once(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        probe=lambda: DaemonProbe(
            state="UP" if up else "OFFLINE",
            detail="test probe",
        ),
        port=port or TelegramFake(),
        chat_id=chat_id,
    )


def test_daemon_up_sends_nothing() -> None:
    store = _Store()
    port = TelegramFake()

    result = _run(store, up=True, port=port)

    assert result.action == "NONE"
    assert port.sent == []
    assert WATCHDOG_STATE_KEY not in store.state


def test_daemon_down_sends_one_alert_and_repeats_are_noops() -> None:
    store = _Store()
    port = TelegramFake()

    first = _run(store, up=False, port=port)
    second = _run(store, up=False, port=port)

    assert first.action == "ALERT_SENT"
    assert second.action == "NONE"
    assert len(port.sent) == 1
    text = port.sent[0]["text"]
    assert text.splitlines()[0] == "系统降级 · OFT 守护进程"
    assert "state: DEGRADED" in text
    assert "reason_code: DAEMON_DOWN" in text
    assert TOKEN_SENTINEL not in text
    assert store.state[WATCHDOG_STATE_KEY].startswith("OUTAGE:")


def test_recovery_is_sent_once_only_after_a_prior_outage() -> None:
    store = _Store()
    port = TelegramFake()

    # Up before any outage is silent.
    assert _run(store, up=True, port=port).action == "NONE"
    assert port.sent == []

    _run(store, up=False, port=port)
    recovered = _run(store, up=True, port=port)
    again = _run(store, up=True, port=port)

    assert recovered.action == "RECOVERY_SENT"
    assert again.action == "NONE"
    assert len(port.sent) == 2
    assert port.sent[1]["text"].splitlines()[0] == "系统恢复 · OFT 守护进程"
    assert "state: RECOVERED" in port.sent[1]["text"]
    assert store.state[WATCHDOG_STATE_KEY] == "UP"


def test_outage_state_survives_a_restart() -> None:
    store = _Store()
    first_port = TelegramFake()
    _run(store, up=False, port=first_port)

    restarted_port = TelegramFake()
    result = _run(store, up=False, port=restarted_port)

    assert result.action == "NONE"
    assert restarted_port.sent == []
    assert len(first_port.sent) == 1


def test_a_failed_send_records_no_state_and_retries_next_run() -> None:
    store = _Store()
    failing = TelegramFake(fail_send_times=1)
    first = _run(store, up=False, port=failing)

    assert first.action == "ALERT_FAILED"
    # The outage was not recorded, so the next scheduled run alerts again.
    assert store.state.get(WATCHDOG_STATE_KEY) is None

    second = _run(store, up=False, port=TelegramFake())
    assert second.action == "ALERT_SENT"


def test_no_private_target_is_a_quiet_noop() -> None:
    store = _Store()
    port = TelegramFake()

    result = _run(store, up=False, port=port, chat_id=None)

    assert result.action == "NONE"
    assert "private chat" in (result.detail or "")
    assert port.sent == []


def test_private_chat_resolution_is_fail_closed() -> None:
    assert resolve_private_chat((1001,)) == 1001
    assert resolve_private_chat((-100200,)) is None
    assert resolve_private_chat((1001, 1002)) is None
    assert resolve_private_chat(()) is None
    assert resolve_private_chat((1001, -100200)) == 1001


def test_overlapping_runs_serialize_the_probe_and_send(tmp_path: Path, monkeypatch: Any) -> None:
    import oft.application.telegram_watchdog as watchdog_module

    store = _Store()
    port = TelegramFake()
    lock_path = tmp_path / "telegram-watchdog.lock"
    first_probe_entered = threading.Event()
    release_first = threading.Event()
    probes: list[str] = []
    actions: list[str] = []

    def probe() -> DaemonProbe:
        probes.append("probe")
        if len(probes) == 1:
            first_probe_entered.set()
            assert release_first.wait(5)
        return DaemonProbe(state="OFFLINE", detail="test probe")

    real_lock = watchdog_module._exclusive_lock
    second_attempt = threading.Event()
    attempts: list[int] = []

    @contextmanager
    def tracking_lock(path: Path) -> Iterator[None]:
        attempts.append(1)
        if len(attempts) == 2:
            second_attempt.set()
        with real_lock(path):
            yield

    monkeypatch.setattr(watchdog_module, "_exclusive_lock", tracking_lock)

    def run() -> None:
        actions.append(
            run_watchdog_once(
                store=store,  # type: ignore[arg-type]
                clock=FixedClock(NOW),
                probe=probe,
                port=port,
                chat_id=CHAT_ID,
                lock_path=lock_path,
            ).action
        )

    first = threading.Thread(target=run)
    first.start()
    assert first_probe_entered.wait(2)

    # The first run holds the exclusive lock and is still inside its probe, so
    # the second run cannot probe or send until the first one finishes.
    second = threading.Thread(target=run)
    second.start()
    assert second_attempt.wait(2)
    assert probes == ["probe"]
    assert port.sent == []

    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert len(probes) == 2
    assert sorted(actions) == ["ALERT_SENT", "NONE"]
    assert len(port.sent) == 1
    # The lock file is a zero-byte sentinel and never carries a secret.
    assert lock_path.exists()
    assert lock_path.read_bytes() == b""


def test_an_unavailable_lock_is_a_bounded_unknown_result(tmp_path: Path) -> None:
    store = _Store()
    port = TelegramFake()
    blocker = tmp_path / "lock-parent"
    blocker.write_text("not a directory")

    result = run_watchdog_once(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        probe=lambda: DaemonProbe(state="OFFLINE", detail="test probe"),
        port=port,
        chat_id=CHAT_ID,
        lock_path=blocker / "telegram-watchdog.lock",
    )

    assert result.daemon_state == "UNKNOWN"
    assert result.action == "NONE"
    assert "lock" in (result.detail or "")
    assert port.sent == []


def test_the_windows_fallback_serializes_in_process(tmp_path: Path, monkeypatch: Any) -> None:
    import oft.application.telegram_watchdog as watchdog_module

    monkeypatch.setattr(watchdog_module, "_FCNTL", None)
    lock_path = tmp_path / "telegram-watchdog.lock"
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_done = threading.Event()
    order: list[str] = []

    def hold() -> None:
        with watchdog_module._exclusive_lock(lock_path):
            order.append("first")
            entered.set()
            assert release.wait(5)

    def wait() -> None:
        second_started.set()
        with watchdog_module._exclusive_lock(lock_path):
            order.append("second")
        second_done.set()

    first = threading.Thread(target=hold)
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=wait)
    second.start()
    assert second_started.wait(2)
    # The fallback holds a process-wide lock, so the second run cannot enter.
    assert not second_done.wait(0.2)
    assert order == ["first"]
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert order == ["first", "second"]
