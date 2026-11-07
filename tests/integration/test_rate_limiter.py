"""Conservative global Binance rate limiting, cooldowns, and budgets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oft.application.rate_limiter import (
    BACKFILL_MAX_CONCURRENCY,
    COOLDOWN_418_SECONDS,
    COOLDOWN_429_SECONDS,
    DEFAULT_ENDPOINT_WEIGHT,
    WS_MAX_STREAMS_PER_CONNECTION,
    BackfillConcurrencyLimiter,
    GlobalRateLimiter,
    ListAlertSink,
    TokenBucket,
    WsSubscriptionBudget,
    clamp_backfill_concurrency,
    endpoint_weight,
)
from oft.clock import FixedClock

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)


def test_endpoint_weights_are_explicit_and_fall_back() -> None:
    assert endpoint_weight("/fapi/v1/aggTrades") > endpoint_weight("/fapi/v1/time")
    assert endpoint_weight("/fapi/v1/klines") > endpoint_weight("/fapi/v1/exchangeInfo")
    assert endpoint_weight("/fapi/v1/unknown") == DEFAULT_ENDPOINT_WEIGHT
    assert endpoint_weight("/fapi/v1/aggTrades?symbol=BTCUSDT&limit=1000") > 0


def test_token_bucket_refuses_when_exhausted() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1)
    assert bucket.try_acquire(6, NOW) is True
    assert bucket.try_acquire(6, NOW) is False
    assert bucket.retry_after(6, NOW) > 0


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1)
    assert bucket.try_acquire(10, NOW) is True
    assert bucket.try_acquire(1, NOW) is False
    assert bucket.try_acquire(1, NOW + timedelta(seconds=1)) is True
    assert bucket.try_acquire(1, NOW + timedelta(seconds=1)) is False


def test_a_large_weight_never_exceeds_the_bucket_capacity() -> None:
    bucket = TokenBucket(capacity=10, refill_per_second=1)
    assert bucket.try_acquire(11, NOW) is False


def test_global_limiter_uses_the_conservative_ceiling() -> None:
    limiter = GlobalRateLimiter(clock=FixedClock(NOW))
    # The default capacity is 70% of Binance's 2400/min weight budget.
    assert limiter.capacity == 1680
    decision = limiter.acquire("/fapi/v1/aggTrades", weight=20)
    assert decision.allowed is True
    assert decision.weight == 20


def test_global_limiter_denies_with_retry_after_when_weight_is_gone() -> None:
    limiter = GlobalRateLimiter(clock=FixedClock(NOW), capacity=20, refill_per_second=1)
    assert limiter.acquire("/fapi/v1/aggTrades", weight=20).allowed is True
    denied = limiter.acquire("/fapi/v1/aggTrades", weight=20)
    assert denied.allowed is False
    assert denied.reason == "WEIGHT"
    assert denied.retry_after_seconds > 0


def test_429_triggers_cooldown_and_an_alert() -> None:
    sink = ListAlertSink()
    limiter = GlobalRateLimiter(clock=FixedClock(NOW), alert_sink=sink)
    limiter.note_status(429, endpoint="/fapi/v1/aggTrades")

    assert limiter.in_cooldown()
    assert limiter.cooldown_until == NOW + timedelta(seconds=COOLDOWN_429_SECONDS)
    assert len(sink.alerts) == 1
    event, payload = sink.alerts[0]
    assert event == "binance.rate_limit"
    assert payload["status"] == 429
    assert payload["cooldown_seconds"] == COOLDOWN_429_SECONDS


def test_418_triggers_a_longer_ban_and_an_alert() -> None:
    sink = ListAlertSink()
    limiter = GlobalRateLimiter(clock=FixedClock(NOW), alert_sink=sink)
    limiter.note_status(418, endpoint="/fapi/v1/aggTrades")

    assert limiter.in_cooldown()
    assert limiter.cooldown_until == NOW + timedelta(seconds=COOLDOWN_418_SECONDS)
    assert sink.alerts[0][1]["status"] == 418


def test_cooldown_blocks_requests_without_consuming_weight() -> None:
    limiter = GlobalRateLimiter(clock=FixedClock(NOW), capacity=100, refill_per_second=1)
    limiter.note_status(429)
    before = limiter.available_weight()
    decision = limiter.acquire("/fapi/v1/klines", weight=5)
    after = limiter.available_weight()

    assert decision.allowed is False
    assert decision.reason == "COOLDOWN"
    assert decision.retry_after_seconds > 0
    assert before == after


def test_repeated_429s_do_not_create_an_alert_storm() -> None:
    sink = ListAlertSink()
    limiter = GlobalRateLimiter(clock=FixedClock(NOW), alert_sink=sink)
    limiter.note_status(429)
    limiter.note_status(429)
    limiter.note_status(418)
    assert len(sink.alerts) == 1


def test_cooldown_expires_and_traffic_resumes() -> None:
    class MutableClock:
        instant = NOW

        def now(self) -> datetime:
            return self.instant

    clock = MutableClock()
    limiter = GlobalRateLimiter(clock=clock, capacity=100, refill_per_second=1)
    limiter.note_status(429)
    clock.instant = NOW + timedelta(seconds=COOLDOWN_429_SECONDS + 1)
    assert limiter.in_cooldown() is False
    assert limiter.acquire("/fapi/v1/klines", weight=1).allowed is True


def test_backfill_concurrency_is_capped_at_four() -> None:
    limiter = BackfillConcurrencyLimiter()
    assert BACKFILL_MAX_CONCURRENCY == 4
    assert [limiter.try_acquire() for _ in range(5)] == [True, True, True, True, False]
    assert limiter.active == 4
    limiter.release()
    assert limiter.active == 3
    assert limiter.try_acquire() is True


def test_requested_backfill_concurrency_is_clamped() -> None:
    assert clamp_backfill_concurrency(1) == 1
    assert clamp_backfill_concurrency(4) == 4
    assert clamp_backfill_concurrency(99) == 4
    assert clamp_backfill_concurrency(0) == 1


def test_ws_subscription_budget_is_bounded() -> None:
    budget = WsSubscriptionBudget()
    assert WS_MAX_STREAMS_PER_CONNECTION >= 1
    assert budget.can_subscribe(WS_MAX_STREAMS_PER_CONNECTION) is True
    assert budget.can_subscribe(WS_MAX_STREAMS_PER_CONNECTION + 1) is False
    budget.consume(WS_MAX_STREAMS_PER_CONNECTION - 1)
    assert budget.remaining() == 1
    assert budget.can_subscribe(2) is False
    assert budget.can_subscribe(1) is True
    with pytest.raises(ValueError):
        budget.consume(2)


def test_rest_client_refuses_when_the_global_budget_is_exhausted() -> None:
    import asyncio

    import pytest

    from oft.adapters.binance_rest import EXCHANGE_INFO_PATH, BinanceRestClient, BinanceRestError

    limiter = GlobalRateLimiter(clock=FixedClock(NOW), capacity=1, refill_per_second=0)
    assert limiter.acquire(EXCHANGE_INFO_PATH).allowed is True
    client = BinanceRestClient(rate_limiter=limiter)

    with pytest.raises(BinanceRestError) as raised:
        asyncio.run(client.fetch_exchange_info())
    assert raised.value.code == "BINANCE_RATE_LIMITED"


def test_ws_client_rejects_a_connection_over_the_subscription_budget() -> None:
    import pytest

    from oft.adapters.binance_ws import BinanceWsClient, SubscriptionRejectedError
    from oft.application.rate_limiter import WsSubscriptionBudget

    client = BinanceWsClient(subscription_budget=WsSubscriptionBudget(max_streams=1))
    with pytest.raises(SubscriptionRejectedError, match="budget"):
        client.connect(["btcusdt@aggTrade", "ethusdt@aggTrade"])


def test_418_during_a_429_cooldown_upgrades_the_ban_to_120s() -> None:
    sink = ListAlertSink()
    clock = FixedClock(NOW)
    limiter = GlobalRateLimiter(clock=clock, alert_sink=sink)
    limiter.note_status(429)
    limiter.note_status(418)

    assert limiter.cooldown_until == NOW + timedelta(seconds=COOLDOWN_418_SECONDS)
    assert len(sink.alerts) == 1


def test_concurrent_acquires_never_overdraw_the_bucket() -> None:
    import threading

    limiter = GlobalRateLimiter(clock=FixedClock(NOW), capacity=10, refill_per_second=0)
    results: list[bool] = []
    lock = threading.Lock()

    def take() -> None:
        decision = limiter.acquire("/fapi/v1/aggTrades", weight=1)
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=take) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 10


def test_structlog_alert_sink_is_available_for_the_composition_root() -> None:
    from oft.application.rate_limiter import StructlogAlertSink

    assert StructlogAlertSink() is not None
