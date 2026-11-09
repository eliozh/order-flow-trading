"""Per-provider circuit breaker: 3 consecutive 5xx/429, 60 s open, half-open."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_common import (
    CircuitOpenError,
    LLMAuthError,
    LLMRateLimited,
    LLMSchemaUnsupported,
    LLMTimeoutError,
    LLMTransportError,
)
from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_ledger import BudgetLedger
from oft.application.llm_orchestrator import CircuitBreaker, LLMOrchestrator
from oft.application.llm_validation import schema_text
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult, LLMRole, ProviderCapabilities

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def test_three_consecutive_rate_limits_open_the_circuit() -> None:
    breaker = CircuitBreaker(clock=_MutableClock(NOW))

    for _ in range(3):
        breaker.record_failure("openai")

    with pytest.raises(CircuitOpenError):
        breaker.before_call("openai")


def test_a_success_resets_the_consecutive_count() -> None:
    breaker = CircuitBreaker(clock=_MutableClock(NOW))
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    breaker.record_success("openai")
    breaker.record_failure("openai")
    breaker.record_failure("openai")

    breaker.before_call("openai")  # still closed


def test_the_circuit_closes_after_the_cooldown() -> None:
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure("openai")

    clock.instant = NOW + timedelta(seconds=61)
    breaker.before_call("openai")  # half-open: one call is allowed
    breaker.record_success("openai")
    breaker.before_call("openai")


def test_a_failure_in_half_open_reopens_immediately() -> None:
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure("openai")
    clock.instant = NOW + timedelta(seconds=61)
    breaker.before_call("openai")

    breaker.record_failure("openai")

    with pytest.raises(CircuitOpenError):
        breaker.before_call("openai")


def test_providers_have_independent_breakers() -> None:
    breaker = CircuitBreaker(clock=_MutableClock(NOW))
    for _ in range(3):
        breaker.record_failure("openai")

    with pytest.raises(CircuitOpenError):
        breaker.before_call("openai")
    breaker.before_call("anthropic")


class _AlwaysLimitedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        raise LLMRateLimited("rate limited", status=429)

    def estimate_cost(self, request: Any, model: str) -> Any:
        raise NotImplementedError


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _request() -> LLMRequest:
    return LLMRequest(
        role=LLMRole.ANALYSIS,
        model="gpt-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
        request_id="REQ-1",
    )


def test_open_circuit_fails_fast_without_calling_the_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _AlwaysLimitedProvider()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=_MutableClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    for _ in range(3):
        orchestrator.breaker.record_failure("openai")

    with pytest.raises(CircuitOpenError) as caught:
        orchestrator.complete_with_validation(
            request=_request(), decision_id="DEC-OPEN", role=LLMRole.ANALYSIS
        )
    assert caught.value.code == "LLM_RATE_LIMITED"
    assert caught.value.message == "circuit open"
    assert provider.calls == 0


def test_repeated_rate_limits_eventually_open_through_the_orchestrator(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = _AlwaysLimitedProvider()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=_MutableClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    for index in range(3):
        with pytest.raises((LLMRateLimited, CircuitOpenError)):
            orchestrator.complete_with_validation(
                request=_request(), decision_id=f"DEC-{index}", role=LLMRole.ANALYSIS
            )

    assert orchestrator.breaker.is_open("openai")


def test_auth_failures_do_not_open_the_circuit() -> None:
    breaker = CircuitBreaker(clock=_MutableClock(NOW))
    for _ in range(10):
        breaker.record_failure("openai", auth=True)
    breaker.before_call("openai")


def test_open_seconds_defaults_to_sixty() -> None:
    assert CircuitBreaker(clock=_MutableClock(NOW)).open_seconds == 60.0
    assert CircuitBreaker(clock=_MutableClock(NOW)).threshold == 3


def test_llm_auth_error_is_not_counted_by_the_breaker() -> None:
    breaker = CircuitBreaker(clock=_MutableClock(NOW))
    breaker.record_failure("openai", auth=True)
    assert breaker.consecutive_failures("openai") == 0
    assert breaker.is_open("openai") is False
    breaker.before_call("openai")  # still admissible


class _RaisingProvider:
    provider = "openai"

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        raise self._error

    def estimate_cost(self, request: Any, model: str) -> Any:
        raise NotImplementedError


class _ExhaustedSemaphore:
    def acquire(self, timeout: float | None = None) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("an unacquired semaphore must not be released")


class _StubRuntime:
    """Minimal runtime exposing a breaker and one exhausted semaphore."""

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker
        self._semaphore = _ExhaustedSemaphore()

    def semaphore(self, provider: str) -> Any:
        return self._semaphore


def _open_then_cooldown(breaker: CircuitBreaker, clock: _MutableClock) -> None:
    for _ in range(3):
        breaker.record_failure("openai")
    assert breaker.is_open("openai")
    clock.instant = NOW + timedelta(seconds=61)


@pytest.mark.parametrize(
    "error",
    [
        LLMAuthError("bad key"),
        LLMSchemaUnsupported("schema not supported"),
        RuntimeError("unexpected store failure"),
    ],
)
def test_every_early_exit_releases_the_half_open_probe(
    tmp_path: Path, error: BaseException
) -> None:
    """A half-open probe that exits without a verdict must not lock the provider (F1)."""
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    _open_then_cooldown(breaker, clock)
    store = _store(tmp_path)
    orchestrator = LLMOrchestrator(
        store=store,
        clock=clock,
        provider=_RaisingProvider(error),
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
        breaker=breaker,
    )

    with pytest.raises(type(error)):
        orchestrator.complete_with_validation(
            request=_request(), decision_id="DEC-EARLY", role=LLMRole.ANALYSIS
        )

    # The probe is resolved, so the next caller is admitted instead of 429ing
    # for the rest of the process lifetime.
    breaker.before_call("openai")


def test_a_stale_permit_does_not_release_a_newer_half_open_probe() -> None:
    """A permit admitted while closed must not clear a later probe's flag (N1).

    The stale call outlives the cooldown and abandons after a newer caller has
    become the single half-open probe. Its release is permit-scoped by the
    generation it was admitted under, so the newer probe stays exclusive.
    """
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    stale = breaker.before_call("openai")  # admitted while the circuit is closed

    _open_then_cooldown(breaker, clock)
    probe = breaker.before_call("openai")  # the one legitimate half-open probe

    stale.abandon()

    with pytest.raises(CircuitOpenError):
        breaker.before_call("openai")

    probe.success()
    breaker.before_call("openai")


def test_a_semaphore_timeout_releases_the_half_open_probe(tmp_path: Path) -> None:
    """Semaphore exhaustion after ``before_call`` must not leak the probe (F1)."""
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    _open_then_cooldown(breaker, clock)
    store = _store(tmp_path)
    orchestrator = LLMOrchestrator(
        store=store,
        clock=clock,
        provider=_RaisingProvider(LLMRateLimited("never called")),
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
        runtime=_StubRuntime(breaker),  # type: ignore[arg-type]
    )

    with pytest.raises(LLMTimeoutError):
        orchestrator.complete_with_validation(
            request=_request(), decision_id="DEC-SEM-PROBE", role=LLMRole.ANALYSIS
        )

    breaker.before_call("openai")


def test_a_breaker_opening_between_retries_keeps_the_real_error(
    tmp_path: Path,
) -> None:
    """The failure that opened the circuit is recorded and re-raised (S4)."""
    store = _store(tmp_path)
    provider = _RaisingProvider(LLMTransportError("boom", status=503, retryable=True))
    orchestrator = LLMOrchestrator(
        store=store,
        clock=_MutableClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    # Two prior failures: the retryable 5xx on attempt 1 trips the breaker.
    orchestrator.breaker.record_failure("openai")
    orchestrator.breaker.record_failure("openai")

    with pytest.raises(LLMTransportError) as caught:
        orchestrator.complete_with_validation(
            request=_request(), decision_id="DEC-OPEN-RETRY", role=LLMRole.ANALYSIS
        )

    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert caught.value.status == 503
    assert provider.calls == 1
    calls = store.list_llm_calls(decision_id="DEC-OPEN-RETRY")
    assert len(calls) == 1
    assert calls[0].status == "ERROR"
    assert calls[0].error_code == "UPSTREAM_UNAVAILABLE"
