"""Conservative shared Binance rate limits, cooldowns, and concurrency budgets.

ADR-0007 requires one global weight budget shared by scans and backfill, a
cooldown (plus alert) on ``429``/``418``, at most four concurrent backfill
downloads, and a bounded WS subscription budget. Time is always injected so
tests never sleep.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog

from oft.application.telegram_events import NotificationSink, build_degradation_event
from oft.clock import SystemClock
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort

BINANCE_WEIGHT_LIMIT_PER_MINUTE = 2400
CONSERVATIVE_LIMIT_PERCENT = 70
CONSERVATIVE_WEIGHT_LIMIT = BINANCE_WEIGHT_LIMIT_PER_MINUTE * CONSERVATIVE_LIMIT_PERCENT // 100
COOLDOWN_429_SECONDS = 60
COOLDOWN_418_SECONDS = 120
BACKFILL_MAX_CONCURRENCY = 4
WS_MAX_STREAMS_PER_CONNECTION = 200
DEFAULT_ENDPOINT_WEIGHT = 5
# The durable cooldown epoch: a restart inside the same episode must neither
# re-emit DEGRADED nor lose the one RECOVERED notice.
RATE_LIMIT_COOLDOWN_STATE_KEY = "telegram.rate_limit.cooldown_until"

# Weights are explicit per endpoint; unknown paths get a safe default. Values
# follow Binance USD-M documented weights for the endpoints this daemon calls.
ENDPOINT_WEIGHTS: dict[str, int] = {
    "/fapi/v1/time": 1,
    "/fapi/v1/exchangeInfo": 1,
    "/fapi/v1/klines": 5,
    "/fapi/v1/aggTrades": 20,
    "/fapi/v1/depth": 10,
    "/fapi/v1/ticker/24hr": 40,
    "/fapi/v1/openInterest": 1,
    "/fapi/v1/fundingRate": 1,
    "/fapi/v1/forceOrders": 20,
}


def endpoint_weight(path: str) -> int:
    """Declared request weight for an endpoint (query string ignored)."""
    clean = path.split("?", 1)[0]
    return ENDPOINT_WEIGHTS.get(clean, DEFAULT_ENDPOINT_WEIGHT)


def _parse_instant(raw: str) -> datetime | None:
    """Parse a persisted UTC timestamp, or ``None`` when it is malformed."""
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class RateLimitDecision:
    """Whether a request may proceed and, if not, why."""

    allowed: bool
    retry_after_seconds: float
    reason: str
    endpoint: str = ""
    weight: int = 0


class TokenBucket:
    """Classic weighted token bucket over an injected clock."""

    def __init__(
        self, *, capacity: float, refill_per_second: float, tokens: float | None = None
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second < 0:
            raise ValueError("refill_per_second must be >= 0")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._tokens = self.capacity if tokens is None else min(float(tokens), self.capacity)
        self._last_refill: datetime | None = None

    def _refresh(self, now: datetime) -> None:
        if self._last_refill is None:
            self._last_refill = now
            return
        elapsed = max(0.0, (now - self._last_refill).total_seconds())
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._last_refill = now

    def available(self, now: datetime) -> float:
        self._refresh(now)
        return self._tokens

    def try_acquire(self, weight: int, now: datetime) -> bool:
        if weight < 0:
            raise ValueError("weight must be >= 0")
        self._refresh(now)
        if weight > self.capacity or self._tokens < weight:
            return False
        self._tokens -= weight
        return True

    def retry_after(self, weight: int, now: datetime) -> float:
        self._refresh(now)
        if weight <= self._tokens:
            return 0.0
        if self.refill_per_second <= 0:
            return float("inf")
        return (weight - self._tokens) / self.refill_per_second


class AlertSink(Protocol):
    def alert(self, event: str, payload: dict[str, Any]) -> None: ...


class StructlogAlertSink:
    """Emit rate-limit alerts through the daemon's structured log."""

    def alert(self, event: str, payload: dict[str, Any]) -> None:
        structlog.get_logger(__name__).error(event, **payload)


class ListAlertSink:
    """Recording sink used by tests and the daemon log adapter."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, dict[str, Any]]] = []

    def alert(self, event: str, payload: dict[str, Any]) -> None:
        self.alerts.append((event, dict(payload)))


class CompositeAlertSink:
    """Fan one alert out to several sinks without letting one block another."""

    def __init__(self, *sinks: AlertSink) -> None:
        self._sinks = tuple(sinks)

    def alert(self, event: str, payload: dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.alert(event, payload)
            except Exception as exc:  # noqa: BLE001 - one sink never blocks the rest
                structlog.get_logger(__name__).warning(
                    "alert_sink_failed",
                    sink=type(sink).__name__,
                    alert_event=event,
                    error_type=type(exc).__name__,
                )


class TelegramAlertSink:
    """Mirror a ``429``/``418`` cooldown (and its recovery) into Telegram.

    The message is a read-only degradation push built from an allowlist. Every
    failure is recorded and swallowed at this boundary: the shared rate limiter
    must keep working even when Telegram is unconfigured, unreachable, or slow.
    """

    def __init__(self, notifications: NotificationSink, *, clock: ClockPort | None = None) -> None:
        self._notifications = notifications
        self._clock = clock or SystemClock()
        self.failures = 0
        self.last_error_code: str | None = None

    def alert(self, event: str, payload: dict[str, Any]) -> None:
        try:
            if event == "binance.rate_limit":
                status = payload.get("status")
                until = payload.get("until")
                self._notifications.enqueue_event(
                    build_degradation_event(
                        subject="provider:binance",
                        state="DEGRADED",
                        reason_code=f"RATE_LIMITED_{status}",
                        occurred_at=str(until),
                        episode=str(until),
                    )
                )
            elif event == "binance.rate_limit_recovered":
                recovered_at = utc_timestamp(self._clock.now())
                self._notifications.enqueue_event(
                    build_degradation_event(
                        subject="provider:binance",
                        state="RECOVERED",
                        reason_code="PROVIDER_RECOVERED",
                        occurred_at=recovered_at,
                        episode=recovered_at,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - never block the rate limiter
            self.failures += 1
            self.last_error_code = type(exc).__name__
            structlog.get_logger(__name__).warning(
                "telegram_rate_limit_alert_failed",
                alert_event=event,
                error_type=type(exc).__name__,
            )


def build_alert_sink(
    notifications: NotificationSink, *, clock: ClockPort | None = None
) -> AlertSink:
    """The documented composition: keep the structlog alert and add Telegram."""
    return CompositeAlertSink(StructlogAlertSink(), TelegramAlertSink(notifications, clock=clock))


class GlobalRateLimiter:
    """Shared weight bucket plus ``429``/``418`` cooldown state."""

    def __init__(
        self,
        *,
        clock: ClockPort | None = None,
        capacity: int = CONSERVATIVE_WEIGHT_LIMIT,
        refill_per_second: float | None = None,
        alert_sink: AlertSink | None = None,
        store: StorePort | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self.capacity = int(capacity)
        rate = refill_per_second if refill_per_second is not None else self.capacity / 60.0
        self._bucket = TokenBucket(capacity=self.capacity, refill_per_second=rate)
        self._cooldown_until: datetime | None = None
        self._cooldown_alerted = False
        self._alert_sink: AlertSink = alert_sink or ListAlertSink()
        self._store = store
        self._lock = threading.RLock()
        # Alert emission is ordered by the state mutation that caused it, while
        # the sink call itself runs outside ``_lock``. ``_alert_sequence`` is
        # allocated under ``_lock``; ``_emitted_sequence`` advances under
        # ``_emit_condition`` so a slow DEGRADED push can never let the same
        # episode's RECOVERED overtake it.
        self._emit_condition = threading.Condition()
        self._alert_sequence = 0
        self._emitted_sequence = 0
        self._restore_cooldown()

    def _now(self) -> datetime:
        return self._clock.now()

    def _restore_cooldown(self) -> None:
        """Restore the persisted cooldown epoch so a restart is not a new episode.

        A still-active epoch keeps ``_cooldown_alerted`` true, so the eventual
        expiry emits exactly one RECOVERED. An already-expired epoch is kept
        until the first check observes it, which emits that recovery.
        """
        if self._store is None:
            return
        try:
            raw = self._store.get_system_state(RATE_LIMIT_COOLDOWN_STATE_KEY)
        except Exception as exc:  # noqa: BLE001 - an unreadable epoch never blocks startup
            structlog.get_logger(__name__).warning(
                "rate_limit_cooldown_unreadable", error_type=type(exc).__name__
            )
            return
        restored = _parse_instant(raw) if raw else None
        if restored is None:
            return
        self._cooldown_until = restored
        self._cooldown_alerted = True

    def _persist_cooldown(self, until: str | None) -> None:
        if self._store is None:
            return
        try:
            self._store.set_system_state(
                RATE_LIMIT_COOLDOWN_STATE_KEY,
                until or "",
                updated_at=utc_timestamp(self._now()),
            )
        except Exception as exc:  # noqa: BLE001 - the in-memory guard still holds
            structlog.get_logger(__name__).warning(
                "rate_limit_cooldown_unwritable", error_type=type(exc).__name__
            )

    def _allocate_alert_sequence_locked(self) -> int:
        """Reserve the next emission ordinal; callers must already hold ``_lock``."""
        sequence = self._alert_sequence
        self._alert_sequence += 1
        return sequence

    def _emit_ordered(self, sequence: int, event: str, payload: dict[str, Any]) -> None:
        """Deliver one alert after every earlier state-change alert.

        The ordinal is allocated under the state lock, so the sink observes a
        DEGRADED before the RECOVERED of the same episode even when the emitter
        threads are scheduled out of order. The wait and the sink call happen
        outside ``_lock``: a slow Telegram/outbox path must never block
        ``acquire()``/``in_cooldown()``.
        """
        with self._emit_condition:
            while self._emitted_sequence < sequence:
                self._emit_condition.wait()
            try:
                self._alert_sink.alert(event, payload)
            finally:
                # Advance even when the sink raises, so a broken sink can never
                # strand every later alert behind this ordinal.
                self._emitted_sequence = sequence + 1
                self._emit_condition.notify_all()

    @property
    def cooldown_until(self) -> datetime | None:
        return self._cooldown_until

    def available_weight(self) -> float:
        with self._lock:
            return self._bucket.available(self._now())

    def in_cooldown(self, now: datetime | None = None) -> bool:
        with self._lock:
            instant = now or self._now()
            cooling, recovered = self._in_cooldown_locked(instant)
        if recovered is not None:
            # Emitted outside the lock: rendering/enqueuing must not block
            # concurrent acquire()/in_cooldown() callers.
            self._alert_recovered(*recovered)
        return cooling

    def _in_cooldown_locked(self, instant: datetime) -> tuple[bool, tuple[int, str] | None]:
        """Return (cooling, recovery) and mutate state under the lock only.

        The durable epoch is cleared under the same lock that clears the
        in-memory epoch, so a ``note_status(429)`` that starts a new episode can
        never persist an epoch the recovery path subsequently blanks.
        """
        if self._cooldown_until is None:
            return False, None
        if instant < self._cooldown_until:
            return True, None
        self._cooldown_until = None
        if not self._cooldown_alerted:
            return False, None
        self._cooldown_alerted = False
        self._persist_cooldown(None)
        return False, (self._allocate_alert_sequence_locked(), utc_timestamp(instant))

    def _alert_recovered(self, sequence: int, recovered_at: str) -> None:
        try:
            self._emit_ordered(
                sequence, "binance.rate_limit_recovered", {"recovered_at": recovered_at}
            )
        except Exception as exc:  # noqa: BLE001 - recovery never breaks traffic
            structlog.get_logger(__name__).warning(
                "rate_limit_recovery_alert_failed", error_type=type(exc).__name__
            )

    def _remaining_cooldown(self, now: datetime) -> float:
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, (self._cooldown_until - now).total_seconds())

    def acquire(
        self, path: str, *, now: datetime | None = None, weight: int | None = None
    ) -> RateLimitDecision:
        with self._lock:
            decision, recovered = self._acquire_locked(path, now=now, weight=weight)
        if recovered is not None:
            self._alert_recovered(*recovered)
        return decision

    def _acquire_locked(
        self, path: str, *, now: datetime | None, weight: int | None
    ) -> tuple[RateLimitDecision, tuple[int, str] | None]:
        instant = now or self._now()
        resolved_weight = endpoint_weight(path) if weight is None else weight
        cooling, recovered = self._in_cooldown_locked(instant)
        if cooling:
            return (
                RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=self._remaining_cooldown(instant),
                    reason="COOLDOWN",
                    endpoint=path,
                    weight=resolved_weight,
                ),
                None,
            )
        if self._bucket.try_acquire(resolved_weight, instant):
            return (
                RateLimitDecision(
                    allowed=True,
                    retry_after_seconds=0.0,
                    reason="OK",
                    endpoint=path,
                    weight=resolved_weight,
                ),
                recovered,
            )
        return (
            RateLimitDecision(
                allowed=False,
                retry_after_seconds=self._bucket.retry_after(resolved_weight, instant),
                reason="WEIGHT",
                endpoint=path,
                weight=resolved_weight,
            ),
            recovered,
        )

    def note_status(
        self, status: int, *, endpoint: str = "", now: datetime | None = None
    ) -> RateLimitDecision | None:
        """Apply a provider response; ``429``/``418`` enter a cooldown once."""
        if status not in (429, 418):
            return None
        with self._lock:
            decision, alert = self._note_status_locked(status, endpoint=endpoint, now=now)
        if alert is not None:
            # The durable epoch is written under the state lock (so a recovery
            # can never blank a newer epoch); only the sink runs outside it, so
            # a slow Telegram/outbox path cannot serialize traffic acquisition.
            sequence, alert_payload = alert
            self._emit_ordered(sequence, "binance.rate_limit", alert_payload)
        return decision

    def _note_status_locked(
        self, status: int, *, endpoint: str, now: datetime | None
    ) -> tuple[RateLimitDecision, tuple[int, dict[str, Any]] | None]:
        instant = now or self._now()
        if self._cooldown_until is not None and instant < self._cooldown_until:
            if status == 418:
                upgraded = max(
                    self._cooldown_until, instant + timedelta(seconds=COOLDOWN_418_SECONDS)
                )
                self._cooldown_until = upgraded
            return (
                RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=self._remaining_cooldown(instant),
                    reason="COOLDOWN",
                    endpoint=endpoint,
                ),
                None,
            )
        seconds = COOLDOWN_429_SECONDS if status == 429 else COOLDOWN_418_SECONDS
        self._cooldown_until = instant + timedelta(seconds=seconds)
        self._cooldown_alerted = True
        payload = {
            "status": status,
            "cooldown_seconds": seconds,
            "endpoint": endpoint,
            "until": utc_timestamp(self._cooldown_until),
        }
        # Persisted while the state lock is held: a concurrent recovery clear is
        # serialized behind this epoch, so it can never blank the newer marker.
        self._persist_cooldown(str(payload["until"]))
        return (
            RateLimitDecision(
                allowed=False,
                retry_after_seconds=float(seconds),
                reason="COOLDOWN",
                endpoint=endpoint,
            ),
            (self._allocate_alert_sequence_locked(), payload),
        )


def clamp_backfill_concurrency(requested: int) -> int:
    """Clamp a requested download concurrency into ``1..4``."""
    return max(1, min(int(requested), BACKFILL_MAX_CONCURRENCY))


@dataclass
class BackfillConcurrencyLimiter:
    """Bounded in-process concurrency for archive downloads."""

    max_concurrency: int = BACKFILL_MAX_CONCURRENCY
    _active: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        if self._active >= self.max_concurrency:
            return False
        self._active += 1
        return True

    def release(self) -> bool:
        if self._active <= 0:
            return False
        self._active -= 1
        return True


@dataclass
class WsSubscriptionBudget:
    """Per-connection stream budget; never over-subscribe a socket."""

    max_streams: int = WS_MAX_STREAMS_PER_CONNECTION
    _used: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.max_streams < 1:
            raise ValueError("max_streams must be >= 1")

    def remaining(self) -> int:
        return max(0, self.max_streams - self._used)

    def reset(self) -> None:
        self._used = 0

    def can_subscribe(self, count: int) -> bool:
        return 0 < count <= self.remaining()

    def consume(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        if count > self.remaining():
            raise ValueError("subscription budget exceeded")
        self._used += count
