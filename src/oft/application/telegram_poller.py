"""Dedicated Telegram poller thread and the combined daemon runtime.

The poller is a ``threading.Thread`` with a stop event and a bounded
``getUpdates`` long poll. It is deliberately separate from the scan, trigger,
and management schedulers: a slow or blocked Telegram call cannot delay them.

The offset is persisted in ``system_state`` after each processed update, so a
simulated restart does not replay. A crash between the reply enqueue and the
offset write is safe because the outbox ``event_key`` is unique.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import structlog

from oft.application.telegram_dispatch import OutboxDispatcher, TelegramDispatchScheduler
from oft.application.telegram_service import TelegramService
from oft.domain.telegram import (
    POLL_OFFSET_KEY,
    POLLER_HEARTBEAT_KEY,
    POLLER_LAST_ERROR_KEY,
    POLLER_STATE_CONFLICT,
    POLLER_STATE_DEGRADED,
    POLLER_STATE_KEY,
    POLLER_STATE_RUNNING,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort
from oft.ports.telegram import (
    TELEGRAM_CONFLICT,
    TELEGRAM_NOT_CONFIGURED,
    TELEGRAM_RATE_LIMITED,
    TELEGRAM_TOKEN_INVALID,
    TELEGRAM_UNAUTHORIZED,
    TELEGRAM_UPSTREAM_ERROR,
    TelegramError,
    TelegramPort,
)

LOGGER = structlog.get_logger(__name__)
POLL_INTERVAL_SECONDS = 1.0
LONG_POLL_TIMEOUT_SECONDS = 25
SHUTDOWN_JOIN_SECONDS = 5.0
# An unconfigured channel must not retry/log every second forever. Back off to
# a slow re-check and log only on a state change; the runtime still picks up a
# token rotation without a restart on the next check.
UNCONFIGURED_BACKOFF_SECONDS = 30.0
# Permanent configuration failures: no host call succeeds until the operator
# fixes the token/allowlist, so treat them like "not configured".
CONFIGURATION_ERROR_CODES = frozenset(
    {TELEGRAM_NOT_CONFIGURED, TELEGRAM_TOKEN_INVALID, TELEGRAM_UNAUTHORIZED}
)
TRANSIENT_BACKOFF_MAX_SECONDS = 60.0


class TelegramPoller:
    """A single background long-poll loop with a persisted offset."""

    def __init__(
        self,
        *,
        service: TelegramService,
        store: StorePort,
        clock: ClockPort,
        port_resolver: Callable[[], TelegramPort],
        long_poll_timeout: int = LONG_POLL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._service = service
        self._store = store
        self._clock = clock
        self._port_resolver = port_resolver
        self._long_poll_timeout = max(0, long_poll_timeout)
        self._poll_interval_seconds = max(0.0, poll_interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wait_override: float | None = None
        self._last_config_code: str | None = None
        self._consecutive_failures = 0
        self._last_poller_state: str | None = None
        self._last_poller_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def offset(self) -> int | None:
        raw = self._store.get_system_state(POLL_OFFSET_KEY)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def poll_once(self) -> int:
        """Poll one batch, enqueue replies, and advance the offset.

        The offset advances per successfully handled update and, when no
        handling failure occurred, to ``max_update_id + 1`` so dropped
        (non-text/unparseable) updates are acknowledged instead of replayed.
        """
        offset = self.offset()
        try:
            port = self._port_resolver()
            page = port.get_updates(offset=offset, timeout_seconds=self._long_poll_timeout)
        except TelegramError as exc:
            if exc.code in CONFIGURATION_ERROR_CODES:
                # Not configured, or a permanently invalid token/unauthorized
                # state: do not call the port again for a while, do not log
                # every tick, and do not write heartbeats (health degrades).
                self._wait_override = UNCONFIGURED_BACKOFF_SECONDS
                self._consecutive_failures = 0
                # Persist the enum state only; the raw port code lives in
                # POLLER_LAST_ERROR_KEY so health never matches magic strings.
                self._record_state(POLLER_STATE_DEGRADED, exc.code)
                if self._last_config_code != exc.code:
                    LOGGER.info("telegram_poll_unconfigured", code=exc.code)
                    self._last_config_code = exc.code
                return 0
            self._last_config_code = None
            self._consecutive_failures += 1
            wait_seconds = self._transient_backoff()
            if exc.code == TELEGRAM_RATE_LIMITED and exc.retry_after is not None:
                # The Bot API's retry_after is a floor, exactly as the
                # dispatcher treats a send-side 429.
                wait_seconds = max(wait_seconds, float(exc.retry_after))
            self._wait_override = wait_seconds
            if exc.code == TELEGRAM_CONFLICT:
                # Another long-poller owns this bot token. Back off with the same
                # bounded schedule; a 409 is not retried in a tight loop.
                previous = self._last_poller_state
                self._record_state(POLLER_STATE_CONFLICT, exc.code)
                if previous != POLLER_STATE_CONFLICT:
                    LOGGER.warning("telegram_poll_conflict", code=exc.code)
                return 0
            # Rate-limited or upstream down: stay alive and try again later.
            self._record_state(POLLER_STATE_DEGRADED, exc.code)
            LOGGER.warning("telegram_poll_failed", code=exc.code)
            return 0
        except Exception:  # noqa: BLE001 - the supervisor thread must survive
            self._last_config_code = None
            self._consecutive_failures += 1
            self._wait_override = self._transient_backoff()
            self._record_state(POLLER_STATE_DEGRADED, TELEGRAM_UPSTREAM_ERROR)
            LOGGER.exception("telegram_poll_unexpected_failure")
            return 0
        processed = 0
        handling_failed = False
        for update in sorted(page.updates, key=lambda item: item.update_id):
            if offset is not None and update.update_id < offset:
                continue
            try:
                self._service.handle_update(update)
            except Exception:  # noqa: BLE001 - do not advance past a failed update
                LOGGER.exception("telegram_update_handling_failed", update_id=update.update_id)
                handling_failed = True
                break
            self._store.set_system_state(
                POLL_OFFSET_KEY,
                str(update.update_id + 1),
                updated_at=utc_timestamp(self._clock.now()),
            )
            processed += 1
        if not handling_failed and page.max_update_id is not None:
            candidate = page.max_update_id + 1
            current = self.offset()
            if current is None or candidate > current:
                self._store.set_system_state(
                    POLL_OFFSET_KEY,
                    str(candidate),
                    updated_at=utc_timestamp(self._clock.now()),
                )
        self._wait_override = None
        self._last_config_code = None
        self._consecutive_failures = 0
        self._record_state(POLLER_STATE_RUNNING, None)
        self._heartbeat()
        return processed

    def _transient_backoff(self) -> float:
        base = max(self._poll_interval_seconds, 1.0)
        factor = 1 << min(self._consecutive_failures, 16)
        return min(base * factor, TRANSIENT_BACKOFF_MAX_SECONDS)

    def _heartbeat(self) -> None:
        now = utc_timestamp(self._clock.now())
        self._store.set_system_state(POLLER_HEARTBEAT_KEY, now, updated_at=now)

    def _record_state(self, state: str, error: str | None) -> None:
        """Persist the stable poller state and last reason, only on a change.

        ``state`` is always one of the ``POLLER_STATE_*`` enum values; a raw
        port code (``TELEGRAM_TOKEN_INVALID`` and friends) is stored only in
        ``POLLER_LAST_ERROR_KEY`` so diagnostics never have to match magic
        strings. The heartbeat is written solely after a successful poll; this
        state key is what tells ``/system`` and ``oft doctor`` that the loop is
        retrying (``DEGRADED``/``CONFLICT``) rather than healthy.
        """
        if state == self._last_poller_state and error == self._last_poller_error:
            return
        try:
            now = utc_timestamp(self._clock.now())
            self._store.set_system_state(POLLER_STATE_KEY, state, updated_at=now)
            self._store.set_system_state(POLLER_LAST_ERROR_KEY, error or "", updated_at=now)
        except Exception:  # noqa: BLE001 - diagnostics must not kill the loop
            LOGGER.warning("telegram_poller_state_write_failed")
            return
        self._last_poller_state = state
        self._last_poller_error = error

    def start(self) -> None:
        if self.is_running:
            LOGGER.warning("telegram_poller_already_running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oft-telegram-poller", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - never exit the supervisor loop
                LOGGER.exception("telegram_poller_tick_failed")
            wait_seconds = (
                self._wait_override
                if self._wait_override is not None
                else self._poll_interval_seconds
            )
            self._stop.wait(wait_seconds)

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=SHUTDOWN_JOIN_SECONDS if wait else 0)
            if thread.is_alive():
                # The long poll can outlive the join timeout. Keep the handle so
                # ``is_running`` stays truthful and ``start()`` refuses to spawn
                # a second poller alongside the orphaned one.
                LOGGER.warning("telegram_poller_shutdown_timed_out")
                return
        self._thread = None


class TelegramRuntime:
    """The poller thread plus the 1 s dispatcher scheduler."""

    def __init__(
        self,
        *,
        poller: TelegramPoller,
        dispatcher: OutboxDispatcher,
        dispatch_interval_seconds: int = 1,
        closer: Callable[[], None] | None = None,
    ) -> None:
        self._poller = poller
        self._dispatcher = dispatcher
        self._closer = closer
        self._closed = False
        self._scheduler = TelegramDispatchScheduler(
            dispatcher=dispatcher,
            interval_seconds=dispatch_interval_seconds,
        )

    @property
    def dispatch_running(self) -> bool:
        return self._scheduler.running

    @property
    def poller(self) -> TelegramPoller:
        return self._poller

    def start(self) -> None:
        # Retention is handled by the dispatcher's first (and periodic) tick, so
        # startup has no separate purge path to fail.
        self._scheduler.start()
        self._poller.start()

    def shutdown(self, *, wait: bool = False) -> None:
        self._scheduler.shutdown(wait=wait)
        self._poller.shutdown(wait=wait)

    def close(self) -> None:
        """Release the adapter's HTTP client after the loops have stopped.

        Idempotent and non-raising: the daemon shutdown path calls this after
        ``shutdown``, and a second close must not mask the other scheduler
        shutdowns. A closer failure is logged and swallowed.
        """
        if self._closed:
            return
        self._closed = True
        closer = self._closer
        if closer is None:
            return
        try:
            closer()
        except Exception:  # noqa: BLE001 - shutdown cleanup must not raise
            LOGGER.warning("telegram_runtime_close_failed")

    def poll_once(self) -> int:
        return self._poller.poll_once()

    def dispatch_once(self) -> int:
        return self._dispatcher.dispatch_once()


__all__ = [
    "LONG_POLL_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "UNCONFIGURED_BACKOFF_SECONDS",
    "TelegramPoller",
    "TelegramRuntime",
]
