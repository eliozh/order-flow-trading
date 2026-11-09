"""HTTP 429/5xx mapping and the orchestrator's single transport retry (F5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from oft.adapters.llm_anthropic import AnthropicAdapter
from oft.adapters.llm_common import LLMRateLimited, LLMTransportError
from oft.adapters.llm_deepseek import DeepSeekAdapter
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_ledger import BudgetLedger
from oft.application.llm_orchestrator import LLMOrchestrator
from oft.application.llm_validation import schema_text, validate_output
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMRole

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "providers"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
SENTINEL = "sk-SENTINEL-DO-NOT-LEAK"
ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "deepseek": DeepSeekAdapter,
}


def _adapter(provider: str, transport: httpx.BaseTransport) -> Any:
    return ADAPTERS[provider](
        api_key=SENTINEL,
        base_url=f"https://api.{provider}.test",
        transport=transport,
    )


def _request(*, model: str = "fixture-model") -> LLMRequest:
    return LLMRequest(
        role=LLMRole.ANALYSIS,
        model=model,
        system_prompt="system prompt",
        user_prompt="user prompt",
        schema_json=schema_text(),
        input_hash="input-hash",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="prompt-hash",
        timeout_seconds=5.0,
        request_id="REQ-TEST",
    )


def _error_transport(status: int, capture: Callable[[], None] | None = None) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture()
        return httpx.Response(
            status,
            json={"error": {"message": f"provider failure for {SENTINEL}"}},
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_a_429_maps_to_a_retryable_rate_limit_with_a_redacted_message(provider: str) -> None:
    with pytest.raises(LLMRateLimited) as caught:
        _adapter(provider, _error_transport(429)).complete(_request())

    assert caught.value.retryable is True
    assert caught.value.code == "LLM_RATE_LIMITED"
    assert SENTINEL not in caught.value.message
    assert "[REDACTED]" in caught.value.message


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_a_503_maps_to_a_retryable_transport_error_with_a_redacted_message(provider: str) -> None:
    with pytest.raises(LLMTransportError) as caught:
        _adapter(provider, _error_transport(503)).complete(_request())

    assert caught.value.retryable is True
    assert caught.value.status == 503
    assert SENTINEL not in caught.value.message
    assert "[REDACTED]" in caught.value.message


@pytest.mark.parametrize("status", [429, 503])
def test_the_orchestrator_retries_once_and_succeeds_on_the_second_request(
    tmp_path: Path, status: int
) -> None:
    valid_body = json.loads((FIXTURES / "openai" / "valid.json").read_text(encoding="utf-8"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, json={"error": {"message": "transient failure"}})
        return httpx.Response(200, json=valid_body)

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    sleeps: list[float] = []
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(NOW),
        provider=_adapter("openai", httpx.MockTransport(handler)),
        budget=BudgetLedger(store=store),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    result = orchestrator.complete_with_validation(
        request=_request(), decision_id="DEC-HTTP", role=LLMRole.ANALYSIS
    )

    assert len(requests) == 2
    assert sleeps == [0.5]
    assert result.outcome.status == "VALID"
    assert validate_output(result.first_result.raw_text, role="ANALYSIS").status == "VALID"


def test_proxy_environment_does_not_alter_the_client_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx must not honour HTTP(S)_PROXY/.netrc/SSL_CERT env (S7)."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    valid_body = json.loads((FIXTURES / "openai" / "valid.json").read_text(encoding="utf-8"))
    captured: dict[str, Any] = {}
    real_client = httpx.Client

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("oft.adapters.llm_common.httpx.Client", spy)

    adapter = _adapter(
        "openai",
        httpx.MockTransport(lambda _request: httpx.Response(200, json=valid_body)),
    )
    adapter.complete(_request())

    assert captured.get("trust_env") is False
