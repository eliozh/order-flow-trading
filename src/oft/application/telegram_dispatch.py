"""Bounded outbox dispatch and the receipt state machine.

The dispatcher claims due rows atomically, sends at most one message per row,
and records Bot API acceptance with the returned ``message_id``. A typed port
failure is retried up to
:data:`oft.domain.telegram.MAX_TELEGRAM_OUTBOX_ATTEMPTS` with bounded
exponential backoff plus injected jitter; the next failure is terminal with
``last_error_code``. A 429 ``retry_after`` is a floor for the next attempt.

Rows for one chat are claimed in enqueue order and sent one at a time; a
per-chat token bucket (injected clock) defers a burst without consuming the
attempt budget, and later same-chat rows wait for an earlier failed/deferred
one. The tick owns a non-blocking lock so an overlapping fire is skipped, and
every failure is swallowed/logged so the scheduler cannot die.
"""

from __future__ import annotations

import json
import math
import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from oft.domain.telegram import (
    DISPATCHER_HEARTBEAT_KEY,
    MAX_TELEGRAM_OUTBOX_ATTEMPTS,
    TELEGRAM_LAST_SEND_AT_KEY,
    TELEGRAM_LOCAL_RATE_LIMITED,
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
# Per-chat token bucket defaults: a short burst is allowed, then one message per
# second per chat. The bucket is clock-injected and keyed by chat, so a burst to
# one chat defers instead of hammering the Bot API while other chats proceed.
DEFAULT_CHAT_BURST = 5.0
DEFAULT_CHAT_REFILL_PER_SECOND = 1.0
MAX_TRACKED_CHATS = 1000
_DELIVERY_FAILED = "TELEGRAM_DELIVERY_FAILED"


def retry_after_delay_seconds(retry_after: int) -> int:
    """Honor a 429 ``retry_after`` as a floor, bounded by the existing cap.

    A zero (or negative) ``retry_after`` still waits at least the backoff base,
    so an unhelpful rate-limit response cannot become a tight retry loop.
    """
    return min(
        max(retry_after, TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS),
        TELEGRAM_RETRY_AFTER_MAX_SECONDS,
    )


def backoff_delay_seconds(attempts: int, *, jitter_fraction: float = 0.0) -> float:
    """Bounded exponential backoff with an injected jitter fraction.

    ``jitter_fraction`` is a value in ``[0, 1]``; the delay is
    ``base + base * fraction`` so it never shrinks below the deterministic
    schedule and stays within the cap. The fraction is injected (the default in
    :class:`OutboxDispatcher` is ``random.random``) so tests are deterministic
    and production is not synchronized across retries.
    """
    base = min(
        float(TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS) * (1 << min(max(attempts, 0), 16)),
        float(TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS),
    )
    fraction = jitter_fraction if math.isfinite(jitter_fraction) else 0.0
    fraction = min(max(fraction, 0.0), 1.0)
    return min(base + base * fraction, float(TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS))


class ChatTokenBucket:
    """A per-chat token bucket driven by an injected instant (no sleeps).

    ``consume(chat_id, now)`` returns ``None`` when a token was available, or
    the instant at which the next token becomes available. Chats are independent.
    The tracked-chat map is bounded; an overflow evicts the oldest entry, which
    only means that chat starts a fresh burst.
    """

    def __init__(
        self,
        *,
        capacity: float = DEFAULT_CHAT_BURST,
        refill_per_second: float = DEFAULT_CHAT_REFILL_PER_SECOND,
    ) -> None:
        if not math.isfinite(capacity) or capacity <= 0:
            raise ValueError("capacity must be a positive finite number")
        if not math.isfinite(refill_per_second) or refill_per_second <= 0:
            raise ValueError("refill_per_second must be a positive finite number")
        self._capacity = float(capacity)
        self._rate = float(refill_per_second)
        self._state: dict[int, tuple[float, datetime]] = {}

    def consume(self, chat_id: int, now: datetime) -> datetime | None:
        is_new_chat = chat_id not in self._state
        tokens, updated = self._state.get(chat_id, (self._capacity, now))
        elapsed = max(0.0, (now - updated).total_seconds())
        tokens = min(self._capacity, tokens + elapsed * self._rate)
        if tokens >= 1.0:
            self._state[chat_id] = (tokens - 1.0, now)
            if is_new_chat and len(self._state) > MAX_TRACKED_CHATS:
                self._state.pop(next(iter(self._state)))
            return None
        self._state[chat_id] = (tokens, now)
        if is_new_chat and len(self._state) > MAX_TRACKED_CHATS:
            self._state.pop(next(iter(self._state)))
        wait_seconds = (1.0 - tokens) / self._rate
        return now + timedelta(seconds=wait_seconds)


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
        jitter: Callable[[], float] | None = None,
        chat_burst: float = DEFAULT_CHAT_BURST,
        chat_refill_per_second: float = DEFAULT_CHAT_REFILL_PER_SECOND,
    ) -> None:
        self._store = store
        self._clock = clock
        self._port_resolver = port_resolver
        self._batch = batch
        self._purge_interval_seconds = max(1, purge_interval_seconds)
        self._purge_batch = max(1, purge_batch)
        self._purge_max_batches = max(1, purge_max_batches)
        # Production jitter is random; tests inject a deterministic source.
        self._jitter = jitter if jitter is not None else random.random
        self._bucket = ChatTokenBucket(
            capacity=chat_burst,
            refill_per_second=chat_refill_per_second,
        )
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
            # Per-chat serialization: rows are claimed in enqueue order within a
            # chat. Once a chat's send fails or is rate-limited, its later rows
            # wait for a later tick instead of jumping the failed one.
            blocked_chats: set[int] = set()
            for row in rows:
                if row.chat_id in blocked_chats:
                    if not self._store.release_telegram_claim(
                        row.event_key, next_attempt_at=now_text
                    ):
                        _log_lost_transition(row.event_key, "release_claim")
                    continue
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
                deferred_until = self._bucket.consume(row.chat_id, self._clock.now())
                if deferred_until is not None:
                    # A local per-chat rate limit is a deferral, not a delivery
                    # attempt: it must not consume the bounded retry budget.
                    self._defer_local(row, deferred_until)
                    blocked_chats.add(row.chat_id)
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
                    blocked_chats.add(row.chat_id)
                except Exception:  # noqa: BLE001 - the scheduler must survive
                    LOGGER.exception(
                        "telegram_dispatch_unexpected_failure", event_key=row.event_key
                    )
                    self._record_failure(row, TELEGRAM_UPSTREAM_ERROR)
                    blocked_chats.add(row.chat_id)
                else:
                    sent_at = utc_timestamp(self._clock.now())
                    if not self._store.mark_telegram_sent(
                        row.event_key,
                        message_id=result.message_id,
                        sent_at=sent_at,
                    ):
                        _log_lost_transition(row.event_key, "sent")
                    # Last accepted send for diagnostics; the outbox itself is
                    # bounded and eventually purged, so this stamp is durable.
                    self._store.set_system_state(
                        TELEGRAM_LAST_SEND_AT_KEY,
                        sent_at,
                        updated_at=sent_at,
                    )
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
        delay: float
        if error_code in (TELEGRAM_RATE_LIMITED, TELEGRAM_NOT_CONFIGURED):
            count_attempt = False
            age_seconds = _attempt_age_seconds(row.created_at, failure_now)
            final = age_seconds >= TELEGRAM_OUTBOX_DEFER_MAX_SECONDS
            if error_code == TELEGRAM_RATE_LIMITED and retry_after is not None and retry_after > 0:
                delay = retry_after_delay_seconds(retry_after)
            else:
                delay = _age_backoff_seconds(age_seconds)
        else:
            count_attempt = True
            final = row.attempts + 1 >= MAX_TELEGRAM_OUTBOX_ATTEMPTS
            delay = backoff_delay_seconds(row.attempts, jitter_fraction=self._jitter())
        next_attempt_at = utc_timestamp(failure_now + timedelta(seconds=delay))
        if not self._store.mark_telegram_attempt_failed(
            row.event_key,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
            final=final,
            count_attempt=count_attempt,
        ):
            _log_lost_transition(row.event_key, "attempt_failed")

    def _defer_local(self, row: TelegramOutboxRecord, next_attempt: datetime) -> None:
        """Re-queue a locally rate-limited row without counting an attempt."""
        if not self._store.mark_telegram_attempt_failed(
            row.event_key,
            error_code=TELEGRAM_LOCAL_RATE_LIMITED,
            next_attempt_at=utc_timestamp(next_attempt),
            final=False,
            count_attempt=False,
        ):
            _log_lost_transition(row.event_key, "rate_limited")

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
    # Retained for the age-based non-counted path; the counted path uses
    # ``backoff_delay_seconds`` so the injected jitter source is honored.
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
    "DEFAULT_CHAT_BURST",
    "DEFAULT_CHAT_REFILL_PER_SECOND",
    "DISPATCH_BATCH",
    "DISPATCH_INTERVAL_SECONDS",
    "ChatTokenBucket",
    "OutboxDispatcher",
    "TelegramDispatchScheduler",
    "backoff_delay_seconds",
    "dispatch_guard",
    "retry_after_delay_seconds",
]
