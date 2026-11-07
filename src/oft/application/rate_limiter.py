"""Conservative shared Binance rate limits, cooldowns, and concurrency budgets.

ADR-0007 requires one global weight budget shared by scans and backfill, a
cooldown (plus alert) on ``429``/``418``, at most four concurrent backfill
downloads, and a bounded WS subscription budget. Time is always injected so
tests never sleep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from oft.clock import SystemClock
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

BINANCE_WEIGHT_LIMIT_PER_MINUTE = 2400
CONSERVATIVE_LIMIT_PERCENT = 70
CONSERVATIVE_WEIGHT_LIMIT = BINANCE_WEIGHT_LIMIT_PER_MINUTE * CONSERVATIVE_LIMIT_PERCENT // 100
COOLDOWN_429_SECONDS = 60
COOLDOWN_418_SECONDS = 120
BACKFILL_MAX_CONCURRENCY = 4
WS_MAX_STREAMS_PER_CONNECTION = 200
DEFAULT_ENDPOINT_WEIGHT = 5

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


class ListAlertSink:
    """Recording sink used by tests and the daemon log adapter."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, dict[str, Any]]] = []

    def alert(self, event: str, payload: dict[str, Any]) -> None:
        self.alerts.append((event, dict(payload)))


class GlobalRateLimiter:
    """Shared weight bucket plus ``429``/``418`` cooldown state."""

    def __init__(
        self,
        *,
        clock: ClockPort | None = None,
        capacity: int = CONSERVATIVE_WEIGHT_LIMIT,
        refill_per_second: float | None = None,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self.capacity = int(capacity)
        rate = refill_per_second if refill_per_second is not None else self.capacity / 60.0
        self._bucket = TokenBucket(capacity=self.capacity, refill_per_second=rate)
        self._cooldown_until: datetime | None = None
        self._alert_sink: AlertSink = alert_sink or ListAlertSink()

    def _now(self) -> datetime:
        return self._clock.now()

    @property
    def cooldown_until(self) -> datetime | None:
        return self._cooldown_until

    def available_weight(self) -> float:
        return self._bucket.available(self._now())

    def in_cooldown(self, now: datetime | None = None) -> bool:
        instant = now or self._now()
        if self._cooldown_until is None:
            return False
        if instant < self._cooldown_until:
            return True
        self._cooldown_until = None
        return False

    def _remaining_cooldown(self, now: datetime) -> float:
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, (self._cooldown_until - now).total_seconds())

    def acquire(
        self, path: str, *, now: datetime | None = None, weight: int | None = None
    ) -> RateLimitDecision:
        instant = now or self._now()
        resolved_weight = endpoint_weight(path) if weight is None else weight
        if self.in_cooldown(instant):
            return RateLimitDecision(
                allowed=False,
                retry_after_seconds=self._remaining_cooldown(instant),
                reason="COOLDOWN",
                endpoint=path,
                weight=resolved_weight,
            )
        if self._bucket.try_acquire(resolved_weight, instant):
            return RateLimitDecision(
                allowed=True,
                retry_after_seconds=0.0,
                reason="OK",
                endpoint=path,
                weight=resolved_weight,
            )
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=self._bucket.retry_after(resolved_weight, instant),
            reason="WEIGHT",
            endpoint=path,
            weight=resolved_weight,
        )

    def note_status(
        self, status: int, *, endpoint: str = "", now: datetime | None = None
    ) -> RateLimitDecision | None:
        """Apply a provider response; ``429``/``418`` enter a cooldown once."""
        if status not in (429, 418):
            return None
        instant = now or self._now()
        if self._cooldown_until is not None and instant < self._cooldown_until:
            # Already cooling down: do not extend or emit a second alert.
            return RateLimitDecision(
                allowed=False,
                retry_after_seconds=self._remaining_cooldown(instant),
                reason="COOLDOWN",
                endpoint=endpoint,
            )
        seconds = COOLDOWN_429_SECONDS if status == 429 else COOLDOWN_418_SECONDS
        self._cooldown_until = instant + timedelta(seconds=seconds)
        self._alert_sink.alert(
            "binance.rate_limit",
            {
                "status": status,
                "cooldown_seconds": seconds,
                "endpoint": endpoint,
                "until": utc_timestamp(self._cooldown_until),
            },
        )
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=float(seconds),
            reason="COOLDOWN",
            endpoint=endpoint,
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
