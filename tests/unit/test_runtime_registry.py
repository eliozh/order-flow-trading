"""Shared runtime registry: breaker + per-provider semaphores across orchestrators (H1, M14)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_common import CircuitOpenError, LLMRateLimited
from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_ledger import BudgetLedger
from oft.application.llm_orchestrator import (
    CircuitBreaker,
    LLMOrchestrator,
    LLMRuntimeRegistry,
)
from oft.application.llm_validation import schema_text
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult, LLMRole, ProviderCapabilities

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class _LimitedProvider:
    provider = "openai"

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


def _orchestrator(
    store: SqliteStore,
    provider: Any,
    runtime: LLMRuntimeRegistry,
    clock: _MutableClock | None = None,
) -> LLMOrchestrator:
    return LLMOrchestrator(
        store=store,
        clock=clock or _MutableClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
        runtime=runtime,
    )


def test_two_orchestrators_accumulate_failures_and_open_the_shared_circuit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    runtime = LLMRuntimeRegistry(clock=_MutableClock(NOW))
    first = _LimitedProvider()
    second = _LimitedProvider()
    third = _LimitedProvider()

    with pytest.raises((LLMRateLimited, CircuitOpenError)):
        _orchestrator(store, first, runtime).complete_with_validation(
            request=_request(), decision_id="DEC-A", role=LLMRole.ANALYSIS
        )
    # The second orchestrator shares the breaker: the accumulated failures open it.
    with pytest.raises((LLMRateLimited, CircuitOpenError)):
        _orchestrator(store, second, runtime).complete_with_validation(
            request=_request(), decision_id="DEC-B", role=LLMRole.ANALYSIS
        )
    assert runtime.breaker.is_open("openai") is True

    with pytest.raises(CircuitOpenError):
        _orchestrator(store, third, runtime).complete_with_validation(
            request=_request(), decision_id="DEC-C", role=LLMRole.ANALYSIS
        )
    assert third.calls == 0


def test_two_orchestrators_share_one_semaphore_slot_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_max_concurrency_deepseek=1,
    )
    runtime = LLMRuntimeRegistry(clock=_MutableClock(NOW), settings=settings)
    provider = _LimitedProvider()
    provider.provider = "deepseek"
    a = _orchestrator(store, provider, runtime)
    b = _orchestrator(store, provider, runtime)

    semaphore = a._semaphore("deepseek")
    assert semaphore is b._semaphore("deepseek")
    assert semaphore.acquire(blocking=False) is True
    # The only slot is held; every orchestrator sees the same exhausted limiter.
    assert b._semaphore("deepseek").acquire(blocking=False) is False
    semaphore.release()


def test_half_open_admits_exactly_one_probe_under_concurrency() -> None:
    clock = _MutableClock(NOW)
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure("openai")
    clock.instant = NOW + timedelta(seconds=61)

    admitted: list[int] = []
    rejected: list[int] = []

    def call(index: int) -> None:
        try:
            breaker.before_call("openai")
            admitted.append(index)
        except CircuitOpenError:
            rejected.append(index)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 1
    assert len(rejected) == 9
    breaker.record_success("openai")
    breaker.before_call("openai")  # closed again


def test_the_registry_is_shared_through_the_real_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production API wiring injects the container registry (H1/F12).

    Each request builds a new probe service; only a registry injected through
    ``build_container`` makes accumulated failures open the shared circuit.
    """
    from fastapi.testclient import TestClient

    from oft.api.app import create_app
    from oft.ports.llm import ProbeOutcome

    class _RateLimitedProbe:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(provider="openai", strict_schema=True)

        def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
            return ProbeOutcome(
                strict_schema=False,
                notes="rate limited",
                error_code="LLM_RATE_LIMITED",
            )

    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: _RateLimitedProbe())
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        openai_api_key="sk-test",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
    )
    app = create_app(settings)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    try:
        for _ in range(3):
            response = client.post(
                "/api/v1/llm/test",
                json={"provider": "openai", "role": "ANALYSIS"},
                headers=auth,
            )
            assert response.status_code == 200, response.text
    finally:
        client.close()

    assert app.state.container.llm_runtime.breaker.is_open("openai") is True
