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
from oft.domain.telegram import POLL_OFFSET_KEY, POLLER_HEARTBEAT_KEY
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort
from oft.ports.telegram import TELEGRAM_NOT_CONFIGURED, TelegramError, TelegramPort

LOGGER = structlog.get_logger(__name__)
POLL_INTERVAL_SECONDS = 1.0
LONG_POLL_TIMEOUT_SECONDS = 25
SHUTDOWN_JOIN_SECONDS = 5.0
# An unconfigured channel must not retry/log every second forever. Back off to
# a slow re-check and log only on a state change; the runtime still picks up a
# token rotation without a restart on the next check.
UNCONFIGURED_BACKOFF_SECONDS = 30.0


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
        self._unconfigured_logged = False

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
        """Poll one batch, enqueue replies, and advance the offset per update."""
        offset = self.offset()
        try:
            port = self._port_resolver()
            updates = port.get_updates(offset=offset, timeout_seconds=self._long_poll_timeout)
        except TelegramError as exc:
            if exc.code == TELEGRAM_NOT_CONFIGURED:
                # The default paper/unconfigured install: no token yet. Do not
                # call the port again for a while, do not log every tick, and do
                # not write heartbeats (the health check short-circuits too).
                self._wait_override = UNCONFIGURED_BACKOFF_SECONDS
                if not self._unconfigured_logged:
                    LOGGER.info("telegram_poll_unconfigured", code=exc.code)
                    self._unconfigured_logged = True
                return 0
            self._wait_override = None
            self._unconfigured_logged = False
            # Rate-limited or upstream down: stay alive and try again next tick.
            LOGGER.warning("telegram_poll_failed", code=exc.code)
            self._heartbeat()
            return 0
        except Exception:  # noqa: BLE001 - the supervisor thread must survive
            self._wait_override = None
            self._unconfigured_logged = False
            LOGGER.exception("telegram_poll_unexpected_failure")
            self._heartbeat()
            return 0
        processed = 0
        for update in sorted(updates, key=lambda item: item.update_id):
            if offset is not None and update.update_id < offset:
                continue
            try:
                self._service.handle_update(update)
            except Exception:  # noqa: BLE001 - do not advance past a failed update
                LOGGER.exception("telegram_update_handling_failed", update_id=update.update_id)
                break
            self._store.set_system_state(
                POLL_OFFSET_KEY,
                str(update.update_id + 1),
                updated_at=utc_timestamp(self._clock.now()),
            )
            processed += 1
        self._wait_override = None
        self._unconfigured_logged = False
        self._heartbeat()
        return processed

    def _heartbeat(self) -> None:
        now = utc_timestamp(self._clock.now())
        self._store.set_system_state(POLLER_HEARTBEAT_KEY, now, updated_at=now)

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
