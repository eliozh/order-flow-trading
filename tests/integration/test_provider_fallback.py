"""DeepSeek degradation, API surfaces, and secret-leak negatives (D8/D9/D11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from oft.adapters.llm_deepseek import DeepSeekAdapter
from oft.adapters.sqlite_store import database_path
from oft.api.app import create_app
from oft.config import Settings
from oft.ports.llm import ProbeOutcome

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = (ROOT / "fixtures" / "llm" / "analysis_valid.json").read_text(encoding="utf-8")
SENTINEL = "sk-SENTINEL-DO-NOT-LEAK"


class _DegradedAdapter:
    def __init__(self) -> None:
        self.models: list[str] = []

    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        self.models.append(model)
        return ProbeOutcome(
            strict_schema=False,
            degraded=True,
            notes="degraded: JSON mode fallback",
            latency_ms=7,
        )


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "port": 8765,
        "data_dir": tmp_path / "data",
        "openai_api_key": SENTINEL,
        "anthropic_api_key": SENTINEL + "-anthropic",
        "deepseek_api_key": SENTINEL + "-deepseek",
        "llm_provider": "openai",
        "llm_model_analysis": "gpt-fixture",
        "llm_model_entry_recheck": "gpt-recheck",
    }
    values.update(overrides)
    return Settings(**values)


def _client(tmp_path: Path, **overrides: Any) -> tuple[TestClient, Settings, Any]:
    settings = _settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    return client, settings, auth


def test_deepseek_json_mode_parses_content_and_rejects_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "ds_1",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": CANONICAL},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    adapter = DeepSeekAdapter(
        api_key=SENTINEL,
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
        json_mode=True,
    )
    from oft.application.llm_validation import schema_text
    from oft.ports.llm import LLMRequest, LLMRole

    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="deepseek-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )

    result = adapter.complete(request)

    assert result.raw_text == CANONICAL
    assert result.truncated is False


def test_deepseek_json_mode_empty_content_is_truncated_and_rejected_without_repair(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.budget_ledger import BudgetLedger
    from oft.application.llm_orchestrator import LLMOrchestrator
    from oft.application.llm_validation import schema_text
    from oft.clock import FixedClock
    from oft.ports.llm import LLMRequest, LLMRole

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "ds_empty_json_mode",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        )

    adapter = DeepSeekAdapter(
        api_key=SENTINEL,
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
        json_mode=True,
    )
    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="deepseek-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
        request_id="REQ-DS-EMPTY",
    )

    # The adapter itself flags the empty JSON-mode body as truncated.
    direct = adapter.complete(request)
    assert direct.raw_text == ""
    assert direct.truncated is True
    before = len(requests)

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(datetime(2026, 11, 9, tzinfo=UTC)),
        provider=adapter,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    result = orchestrator.complete_with_validation(
        request=request, decision_id="DEC-DS-EMPTY", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "REJECTED"
    assert result.outcome.errors[0].code == "LLM_OUTPUT_TRUNCATED"
    assert len(requests) - before == 1


def test_provider_listing_masks_keys_and_shows_the_degraded_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _DegradedAdapter()
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: adapter)
    client, settings, auth = _client(tmp_path)
    try:
        before = client.get("/api/v1/llm/providers", headers=auth)
        probed = client.post(
            "/api/v1/llm/test", json={"provider": "deepseek", "role": "ANALYSIS"}, headers=auth
        )
        after = client.get("/api/v1/llm/providers", headers=auth)
    finally:
        client.close()

    assert before.status_code == 200, before.text
    before_items = {item["provider"]: item for item in before.json()["data"]["items"]}
    assert before_items["deepseek"]["capability"] is None
    assert before_items["deepseek"]["configured"] is True
    assert before_items["openai"]["configured"] is True

    assert probed.status_code == 200, probed.text
    assert probed.json()["data"]["status"] == "DEGRADED"

    items = {item["provider"]: item for item in after.json()["data"]["items"]}
    assert items["deepseek"]["capability"]["status"] == "DEGRADED"
    assert items["deepseek"]["capability"]["notes"] == "degraded: JSON mode fallback"
    assert SENTINEL not in before.text
    assert SENTINEL not in probed.text
    assert SENTINEL not in after.text
    db_bytes = database_path(settings).read_bytes()
    assert SENTINEL.encode() not in db_bytes


def test_llm_test_requires_a_key_and_a_role_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: _DegradedAdapter())
    client, _settings_, auth = _client(tmp_path, openai_api_key=None, llm_model_analysis=None)
    try:
        missing_key = client.post("/api/v1/llm/test", json={"provider": "openai"}, headers=auth)
        missing_model = client.post(
            "/api/v1/llm/test", json={"provider": "anthropic"}, headers=auth
        )
    finally:
        client.close()

    assert missing_key.status_code == 412
    assert missing_key.json()["error"]["code"] == "LLM_PROVIDER_NOT_CONFIGURED"
    assert missing_model.status_code == 412
    assert missing_model.json()["error"]["code"] == "LLM_MODEL_NOT_CONFIGURED"


def test_models_include_the_versioned_price_and_unpriced_role_models(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "version": "2026-11-09",
                "models": [
                    {
                        "provider": "openai",
                        "model": "gpt-priced",
                        "input_per_mtok": "2.50",
                        "output_per_mtok": "10.00",
                        "cache_read_per_mtok": "1.25",
                        "cache_write_per_mtok": "3.00",
                        "source_url": "https://example.invalid",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client, _settings_, auth = _client(tmp_path, llm_price_table=prices)
    try:
        response = client.get("/api/v1/llm/models?provider=openai", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    items = {item["model"]: item for item in response.json()["data"]["items"]}
    assert items["gpt-priced"]["unpriced"] is False
    assert items["gpt-priced"]["input_per_mtok"] == "2.50"
    assert items["gpt-fixture"]["unpriced"] is True


def test_budget_routes_show_set_and_report_usage(tmp_path: Path) -> None:
    client, _settings_, auth = _client(tmp_path)
    try:
        shown = client.get("/api/v1/budget", headers=auth)
        updated = client.put(
            "/api/v1/budget",
            json={"daily_usd": "5.00", "warn_pct": 75},
            headers=auth,
        )
        invalid = client.put("/api/v1/budget", json={}, headers=auth)
        usage = client.get("/api/v1/budget/usage?period=day", headers=auth)
    finally:
        client.close()

    assert shown.status_code == 200, shown.text
    assert shown.json()["data"]["thresholds"]["daily_usd"] == "10.00"
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["thresholds"]["daily_usd"] == "5.00"
    assert updated.json()["data"]["thresholds"]["warn_pct"] == 75.0
    assert invalid.status_code == 422
    assert usage.status_code == 200, usage.text
    assert usage.json()["data"]["call_count"] == 0
    assert usage.json()["data"]["unpriced_count"] == 0
    for response_text in (shown.text, updated.text, usage.text):
        assert SENTINEL not in response_text


def test_a_strict_schema_rejection_retries_once_in_json_mode_and_records_degraded(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.budget_ledger import BudgetLedger
    from oft.application.llm_orchestrator import LLMOrchestrator
    from oft.application.llm_validation import schema_text
    from oft.clock import FixedClock
    from oft.ports.llm import LLMRequest, LLMRole

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if "tools" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "strict schema is not supported"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "ds_degraded",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": CANONICAL},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    adapter = DeepSeekAdapter(
        api_key=SENTINEL,
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(datetime(2026, 11, 9, tzinfo=UTC)),
        provider=adapter,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="deepseek-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )

    result = orchestrator.complete_with_validation(
        request=request, decision_id="DEC-DEGRADED", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "VALID"
    assert len(requests) == 2
    capability = store.get_provider_capability("deepseek")
    assert capability is not None
    assert capability.degraded is True
    assert capability.strict_schema is False
    assert capability.status == "OK"


def test_a_rejection_in_both_modes_does_not_loop(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.budget_ledger import BudgetLedger
    from oft.application.llm_orchestrator import LLMOrchestrator
    from oft.application.llm_validation import schema_text
    from oft.clock import FixedClock
    from oft.ports.llm import LLMRequest, LLMRole, LLMSchemaUnsupported

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "schema is not supported"}})

    adapter = DeepSeekAdapter(
        api_key=SENTINEL,
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(datetime(2026, 11, 9, tzinfo=UTC)),
        provider=adapter,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="deepseek-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )

    import pytest as _pytest

    with _pytest.raises(LLMSchemaUnsupported):
        orchestrator.complete_with_validation(
            request=request, decision_id="DEC-NOLOOP", role=LLMRole.ANALYSIS
        )
    assert len(requests) == 2


def test_a_failed_degraded_retry_records_its_own_typed_failure(tmp_path: Path) -> None:
    """A clone failure keeps its 429 and is recorded, never masked as 422 (F7)."""
    from datetime import UTC, datetime

    from oft.adapters.sqlite_store import SqliteStore
    from oft.application.budget_ledger import BudgetLedger
    from oft.application.llm_orchestrator import LLMOrchestrator
    from oft.application.llm_validation import schema_text
    from oft.clock import FixedClock
    from oft.ports.llm import LLMRateLimited, LLMRequest, LLMRole

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "schema is not supported"}})
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    adapter = DeepSeekAdapter(
        api_key=SENTINEL,
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(datetime(2026, 11, 9, tzinfo=UTC)),
        provider=adapter,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="deepseek-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )

    with pytest.raises(LLMRateLimited) as caught:
        orchestrator.complete_with_validation(
            request=request, decision_id="DEC-CLONE-FAIL", role=LLMRole.ANALYSIS
        )

    assert caught.value.code == "LLM_RATE_LIMITED"
    assert len(requests) == 2
    calls = store.list_llm_calls(decision_id="DEC-CLONE-FAIL")
    assert len(calls) == 1
    assert calls[0].status == "ERROR"
    assert calls[0].error_code == "LLM_RATE_LIMITED"
    assert orchestrator.breaker.consecutive_failures("deepseek") == 1
