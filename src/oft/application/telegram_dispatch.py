"""Bounded outbox dispatch and the receipt state machine.

The dispatcher claims due rows atomically, sends at most one message per row,
and records Bot API acceptance with the returned ``message_id``. A typed port
failure is retried up to
:data:`oft.domain.telegram.MAX_TELEGRAM_OUTBOX_ATTEMPTS`; the next failure is
terminal with ``last_error_code``. The tick owns a non-blocking lock so an
overlapping fire is skipped, and every failure is swallowed/logged so the
scheduler cannot die.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from oft.domain.telegram import (
    DISPATCHER_HEARTBEAT_KEY,
    MAX_TELEGRAM_OUTBOX_ATTEMPTS,
    TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS,
    TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS,
    TELEGRAM_OUTBOX_DEFER_MAX_SECONDS,
    TELEGRAM_OUTBOX_PURGE_BATCH,
    TELEGRAM_OUTBOX_PURGE_INTERVAL_SECONDS,
    TELEGRAM_OUTBOX_PURGE_KEY,
    TELEGRAM_OUTBOX_PURGE_MAX_BATCHES,
    TELEGRAM_OUTBOX_RETENTION_SECONDS,
    TELEGRAM_RETRY_AFTER_MAX_SECONDS,
    truncate_outbound,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort, TelegramOutboxRecord
from oft.ports.telegram import (
    TELEGRAM_NOT_CONFIGURED,
    TELEGRAM_RATE_LIMITED,
    TELEGRAM_UPSTREAM_ERROR,
    TelegramError,
    TelegramPort,
)

LOGGER = structlog.get_logger(__name__)
DISPATCH_BATCH = 10
DISPATCH_INTERVAL_SECONDS = 1
_DELIVERY_FAILED = "TELEGRAM_DELIVERY_FAILED"


def _text_from_payload(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("telegram outbox payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("telegram outbox payload is not an object")
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("telegram outbox payload carries no text")
    return truncate_outbound(text)


class OutboxDispatcher:
    """Drain due Telegram outbox rows with a bounded retry counter."""

    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        port_resolver: Callable[[], TelegramPort],
        batch: int = DISPATCH_BATCH,
        purge_interval_seconds: int = TELEGRAM_OUTBOX_PURGE_INTERVAL_SECONDS,
        purge_batch: int = TELEGRAM_OUTBOX_PURGE_BATCH,
        purge_max_batches: int = TELEGRAM_OUTBOX_PURGE_MAX_BATCHES,
    ) -> None:
        self._store = store
        self._clock = clock
        self._port_resolver = port_resolver
        self._batch = batch
        self._purge_interval_seconds = max(1, purge_interval_seconds)
        self._purge_batch = max(1, purge_batch)
        self._purge_max_batches = max(1, purge_max_batches)
        self._last_purge_at: datetime | None = None
        self._lock = threading.Lock()

    def dispatch_once(self) -> int:
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            now = self._clock.now()
            now_text = utc_timestamp(now)
            self._maybe_purge(now, now_text)
            rows = self._store.claim_due_telegram_outbox(now=now_text, limit=self._batch)
            sent = 0
            for row in rows:
                try:
                    text = _text_from_payload(row.payload_json)
                except ValueError:
                    LOGGER.warning("telegram_outbox_payload_invalid", event_key=row.event_key)
                    if not self._store.mark_telegram_attempt_failed(
                        row.event_key,
                        error_code=_DELIVERY_FAILED,
                        next_attempt_at=now_text,
                        final=True,
                    ):
                        _log_lost_transition(row.event_key, "attempt_failed")
                    continue
                try:
                    port = self._port_resolver()
                    result = port.send_message(
                        chat_id=row.chat_id,
                        text=text,
                        reply_to_message_id=row.reply_to_message_id,
                    )
                except TelegramError as exc:
                    self._record_failure(row, exc.code, retry_after=exc.retry_after)
                except Exception:  # noqa: BLE001 - the scheduler must survive
                    LOGGER.exception(
                        "telegram_dispatch_unexpected_failure", event_key=row.event_key
                    )
                    self._record_failure(row, TELEGRAM_UPSTREAM_ERROR)
                else:
                    if not self._store.mark_telegram_sent(
                        row.event_key,
                        message_id=result.message_id,
                        sent_at=utc_timestamp(self._clock.now()),
                    ):
                        _log_lost_transition(row.event_key, "sent")
                    sent += 1
            if rows:
                # An empty tick writes nothing: the default unconfigured install
                # must not perform ~1 SQLite write per second forever.
                self._store.set_system_state(
                    DISPATCHER_HEARTBEAT_KEY,
                    now_text,
                    updated_at=now_text,
                )
            return sent
        except Exception:  # noqa: BLE001 - never kill the APScheduler job
            LOGGER.exception("telegram_dispatch_tick_failed")
            return 0
        finally:
            self._lock.release()

    def _record_failure(
        self,
        row: TelegramOutboxRecord,
        error_code: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        """Schedule the next attempt with backoff (and honor a 429 ``retry_after``).

        A rate-limit or not-configured failure does not consume the bounded
        attempt budget, but it is not retried forever: the delay grows with the
        row's age and the row becomes terminal ``failed`` after
        :data:`TELEGRAM_OUTBOX_DEFER_MAX_SECONDS`. Other transient failures
        back off exponentially and become terminal at
        :data:`MAX_TELEGRAM_OUTBOX_ATTEMPTS`. Timestamps are stamped from the
        failure instant, not the (possibly long) tick start.
        """
        failure_now = self._clock.now()
        if error_code in (TELEGRAM_RATE_LIMITED, TELEGRAM_NOT_CONFIGURED):
            count_attempt = False
            age_seconds = _attempt_age_seconds(row.created_at, failure_now)
            final = age_seconds >= TELEGRAM_OUTBOX_DEFER_MAX_SECONDS
            if error_code == TELEGRAM_RATE_LIMITED and retry_after is not None and retry_after > 0:
                delay = min(
                    max(retry_after, TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS),
                    TELEGRAM_RETRY_AFTER_MAX_SECONDS,
                )
            else:
                delay = _age_backoff_seconds(age_seconds)
        else:
            count_attempt = True
            final = row.attempts + 1 >= MAX_TELEGRAM_OUTBOX_ATTEMPTS
            delay = _backoff_seconds(row.attempts)
        next_attempt_at = utc_timestamp(failure_now + timedelta(seconds=delay))
        if not self._store.mark_telegram_attempt_failed(
            row.event_key,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
            final=final,
            count_attempt=count_attempt,
        ):
            _log_lost_transition(row.event_key, "attempt_failed")

    def _maybe_purge(self, now: datetime, now_text: str) -> None:
        """Purge old terminal outbox rows at most once per interval.

        The in-memory stamp avoids a database read on every tick; the persisted
        stamp lets a fresh process honor the last purge. Both checks happen
        inside the dispatcher tick lock. Every failure is swallowed/logged so
        retention can never break a tick.
        """
        last = self._last_purge_at
        if last is not None and (now - last).total_seconds() < self._purge_interval_seconds:
            return
        try:
            persisted = self._store.get_system_state(TELEGRAM_OUTBOX_PURGE_KEY)
            if persisted is not None:
                parsed = _parse_timestamp(persisted)
                if parsed is not None:
                    self._last_purge_at = parsed
                    if (now - parsed).total_seconds() < self._purge_interval_seconds:
                        return
            cutoff = utc_timestamp(now - timedelta(seconds=TELEGRAM_OUTBOX_RETENTION_SECONDS))
            for _ in range(self._purge_max_batches):
                if (
                    self._store.purge_terminal_telegram_outbox(
                        before=cutoff, limit=self._purge_batch
                    )
                    < self._purge_batch
                ):
                    break
            self._store.set_system_state(TELEGRAM_OUTBOX_PURGE_KEY, now_text, updated_at=now_text)
            self._last_purge_at = now
        except Exception as exc:  # noqa: BLE001 - retention is best-effort
            LOGGER.warning("telegram_outbox_purge_failed", error_type=type(exc).__name__)


def _parse_timestamp(value: str) -> datetime | None:
    """Parse one ISO timestamp, tolerating a missing/invalid persisted value."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _attempt_age_seconds(created_at: str, now: datetime) -> int:
    """The row's age in whole seconds; an unparseable stamp reads as zero."""
    parsed = _parse_timestamp(created_at)
    if parsed is None:
        return 0
    return max(0, int((now - parsed).total_seconds()))


def _age_backoff_seconds(age_seconds: int) -> int:
    # Grow with the row's age (one doubling per elapsed minute), never below the
    # base and capped so the shift stays cheap and the delay bounded.
    exponent = min(age_seconds // 60, 16)
    return min(
        max(
            TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS * (1 << exponent),
            TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS,
        ),
        TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS,
    )


def _backoff_seconds(attempts: int) -> int:
    # A bounded exponent keeps the shift cheap; the min() clamps the result.
    return min(
        TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS * (1 << min(attempts, 16)),
        TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS,
    )


def _log_lost_transition(event_key: str, transition: str) -> None:
    """A state write returned ``False``: the row left ``sending`` concurrently."""
    LOGGER.warning(
        "telegram_outbox_transition_lost",
        event_key=event_key,
        transition=transition,
    )


class TelegramDispatchScheduler:
    """A 1 s single-instance scheduler around ``dispatch_once``."""

    def __init__(
        self,
        *,
        dispatcher: OutboxDispatcher,
        interval_seconds: int = DISPATCH_INTERVAL_SECONDS,
        timezone: str = "UTC",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._scheduler = BackgroundScheduler(timezone=timezone)
        self._scheduler.add_job(
            dispatch_guard(dispatcher),
            trigger=IntervalTrigger(seconds=interval_seconds, timezone=timezone),
            id="telegram_dispatch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(1, interval_seconds),
        )

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)


def dispatch_guard(dispatcher: OutboxDispatcher) -> Callable[[], None]:
    """Wrap ``dispatch_once`` so an APScheduler job can never raise upward."""

    def _run() -> None:
        try:
            dispatcher.dispatch_once()
        except Exception:  # noqa: BLE001 - the scheduler must keep ticking
            LOGGER.exception("telegram_dispatch_job_failed")

    return _run


__all__ = [
    "DISPATCH_BATCH",
    "DISPATCH_INTERVAL_SECONDS",
    "OutboxDispatcher",
    "TelegramDispatchScheduler",
    "dispatch_guard",
]
