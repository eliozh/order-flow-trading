"""One-shot host-level watchdog for a dead daemon (cron/systemd ``OnFailure=``).

This module is deliberately *not* started by ``daemon start`` and runs no
scheduler: it performs one probe and exits, so it can report an outage that the
daemon's own outbox dispatcher cannot (that dispatcher is dead while the daemon
is down). It sends at most one alert per outage and one recovery after a prior
outage, using a durable epoch in ``system_state`` so repeated runs and restarts
are no-ops. The bot token is never read, printed, or stored here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog

from oft.application.health import DaemonProbe
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort
from oft.ports.telegram import TelegramError, TelegramPort

LOGGER = structlog.get_logger(__name__)

WATCHDOG_STATE_KEY = "telegram.watchdog_state"
_OUTAGE_PREFIX = "OUTAGE:"
_UP_STATE = "UP"
_MAX_DETAIL_CHARS = 200
_ALERT_ACTION = "ALERT_SENT"
_RECOVERY_ACTION = "RECOVERY_SENT"


@dataclass(frozen=True)
class WatchdogResult:
    """The outcome of one watchdog run; never carries a secret."""

    daemon_state: str
    action: str
    detail: str | None = None

    def to_contract(self) -> dict[str, object]:
        return {
            "daemon_state": self.daemon_state,
            "action": self.action,
            "detail": self.detail,
        }


def resolve_private_chat(chat_ids: Sequence[int]) -> int | None:
    """The one positive (private) chat id, or ``None`` when unsafe.

    A negative id is a group/supergroup, whose members would all see the
    message; zero or several eligible ids fail closed exactly like automatic
    pushes.
    """
    private = [
        chat_id
        for chat_id in chat_ids
        if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0
    ]
    return private[0] if len(private) == 1 else None


def run_watchdog_once(
    *,
    store: StorePort,
    clock: ClockPort,
    probe: Callable[[], DaemonProbe],
    port: TelegramPort,
    chat_id: int | None,
) -> WatchdogResult:
    """Probe once, then send at most one alert and one recovery per episode."""
    try:
        persisted = store.get_system_state(WATCHDOG_STATE_KEY)
    except Exception as exc:  # noqa: BLE001 - fail closed on unreadable state
        LOGGER.warning("telegram_watchdog_state_unreadable", error_type=type(exc).__name__)
        return WatchdogResult(
            daemon_state="UNKNOWN",
            action="NONE",
            detail="the persisted watchdog state is unreadable",
        )
    probe_result = probe()
    occurred_at = utc_timestamp(clock.now())
    had_outage = isinstance(persisted, str) and persisted.startswith(_OUTAGE_PREFIX)

    if probe_result.state == "UP":
        if not had_outage:
            return WatchdogResult(daemon_state="UP", action="NONE", detail="no prior outage")
        if chat_id is None:
            return _no_target("UP")
        if not _send(port, chat_id, _recovery_text(occurred_at)):
            return WatchdogResult(
                daemon_state="UP", action="RECOVERY_FAILED", detail="the recovery send failed"
            )
        if not _set_state(store, _UP_STATE, occurred_at):
            return WatchdogResult(
                daemon_state="UP",
                action=_RECOVERY_ACTION,
                detail="the recovery was sent but its state was not persisted",
            )
        return WatchdogResult(daemon_state="UP", action=_RECOVERY_ACTION)

    # The daemon is offline.
    if had_outage:
        return WatchdogResult(
            daemon_state="OFFLINE", action="NONE", detail="already alerted this outage"
        )
    if chat_id is None:
        return _no_target("OFFLINE")
    if not _send(port, chat_id, _alert_text(occurred_at, probe_result.detail)):
        return WatchdogResult(
            daemon_state="OFFLINE", action="ALERT_FAILED", detail="the alert send failed"
        )
    if not _set_state(store, f"{_OUTAGE_PREFIX}{occurred_at}", occurred_at):
        return WatchdogResult(
            daemon_state="OFFLINE",
            action=_ALERT_ACTION,
            detail="the alert was sent but its epoch was not persisted",
        )
    return WatchdogResult(daemon_state="OFFLINE", action=_ALERT_ACTION)


def _no_target(daemon_state: str) -> WatchdogResult:
    return WatchdogResult(
        daemon_state=daemon_state,
        action="NONE",
        detail="no single private chat target is configured",
    )


def _set_state(store: StorePort, value: str, occurred_at: str) -> bool:
    try:
        store.set_system_state(WATCHDOG_STATE_KEY, value, updated_at=occurred_at)
        return True
    except Exception as exc:  # noqa: BLE001 - a failed marker must not crash cron
        LOGGER.warning("telegram_watchdog_state_unwritable", error_type=type(exc).__name__)
        return False


def _send(port: TelegramPort, chat_id: int, text: str) -> bool:
    """Send one one-shot message; a transport failure is logged, never raised."""
    try:
        port.send_message(chat_id=chat_id, text=text)
        return True
    except TelegramError as exc:
        LOGGER.warning("telegram_watchdog_send_failed", error_code=exc.code)
        return False
    except Exception as exc:  # noqa: BLE001 - cron must not see a traceback
        LOGGER.warning("telegram_watchdog_send_failed", error_type=type(exc).__name__)
        return False


def _alert_text(occurred_at: str, detail: object) -> str:
    lines = [
        "系统降级 · OFT 守护进程",
        "state: DEGRADED",
        "reason_code: DAEMON_DOWN",
    ]
    safe_detail = _bounded_detail(detail)
    if safe_detail:
        lines.append(f"detail: {safe_detail}")
    lines.append(f"occurred_at: {occurred_at}")
    return "\n".join(lines)


def _recovery_text(occurred_at: str) -> str:
    return "\n".join(
        [
            "系统恢复 · OFT 守护进程",
            "state: RECOVERED",
            "reason_code: DAEMON_RECOVERED",
            f"occurred_at: {occurred_at}",
        ]
    )


def _bounded_detail(detail: object) -> str:
    """One plain line from the local probe detail (never a token or URL)."""
    if detail is None:
        return ""
    text = " ".join(str(detail).split())
    return text[:_MAX_DETAIL_CHARS]


__all__ = [
    "WATCHDOG_STATE_KEY",
    "WatchdogResult",
    "resolve_private_chat",
    "run_watchdog_once",
]
