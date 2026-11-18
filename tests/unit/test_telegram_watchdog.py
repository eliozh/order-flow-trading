"""The one-shot host-level watchdog: one alert and one recovery per outage."""

from __future__ import annotations

from datetime import UTC, datetime
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
