"""Recorded provider bodies drive each adapter without any network (D2, D12)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from oft.adapters.llm_anthropic import AnthropicAdapter
from oft.adapters.llm_common import LLMAuthError, LLMSchemaUnsupported
from oft.adapters.llm_deepseek import DeepSeekAdapter
from oft.adapters.llm_openai import OpenAIAdapter
from oft.application.llm_validation import schema_text, validate_output
from oft.ports.llm import LLMRequest, LLMRole

PROVIDERS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "providers"
ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "deepseek": DeepSeekAdapter,
}
CASES = ("valid", "refusal", "truncated", "empty_content", "malformed")


def _fixture(provider: str, case: str) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = PROVIDERS_DIR / provider
    body = json.loads((directory / f"{case}.json").read_text(encoding="utf-8"))
    meta = json.loads((directory / f"{case}.meta.json").read_text(encoding="utf-8"))
    return body, meta


def _transport(
    provider: str,
    case: str,
    capture: Callable[[httpx.Request], None] | None = None,
) -> httpx.MockTransport:
    body, meta = _fixture(provider, case)

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture(request)
        return httpx.Response(meta["status"], json=body, headers=meta["headers"])

    return httpx.MockTransport(handler)


def _adapter(provider: str, transport: httpx.BaseTransport) -> Any:
    kwargs: dict[str, Any] = {
        "api_key": "sk-SENTINEL-DO-NOT-LEAK",
        "transport": transport,
    }
    if provider == "anthropic":
        kwargs["base_url"] = "https://api.anthropic.test"
    elif provider == "openai":
        kwargs["base_url"] = "https://api.openai.test"
    else:
        kwargs["base_url"] = "https://api.deepseek.test"
    return ADAPTERS[provider](**kwargs)


def _request(*, role: LLMRole = LLMRole.ANALYSIS) -> LLMRequest:
    return LLMRequest(
        role=role,
        model="fixture-model",
        system_prompt="system prompt",
        user_prompt="user prompt",
        schema_json=schema_text(),
        input_hash="input-hash",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="prompt-hash",
        max_output_tokens=128,
        timeout_seconds=5.0,
        request_id="REQ-TEST",
    )


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_valid_body_parses_to_a_valid_decision(provider: str) -> None:
    result = _adapter(provider, _transport(provider, "valid")).complete(_request())

    assert result.provider == provider
    assert result.refusal is False
    assert result.truncated is False
    assert validate_output(result.raw_text, role="ANALYSIS").status == "VALID"


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_usage_is_read_from_each_provider_body(provider: str) -> None:
    result = _adapter(provider, _transport(provider, "valid")).complete(_request())

    usage = result.usage
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.cache_read_tokens == 100
    if provider == "anthropic":
        assert usage.cache_write_tokens == 20
        assert usage.reasoning_tokens == 0
    else:
        assert usage.cache_write_tokens == 0
        assert usage.reasoning_tokens == 20


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
@pytest.mark.parametrize("case", CASES[1:])
def test_bad_cases_never_parse_to_a_valid_decision(provider: str, case: str) -> None:
    result = _adapter(provider, _transport(provider, case)).complete(_request())

    outcome = validate_output(result.raw_text, role="ANALYSIS")
    assert outcome.status == "REJECTED"


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_truncation_flag_is_set(provider: str) -> None:
    result = _adapter(provider, _transport(provider, "truncated")).complete(_request())

    assert result.truncated is True


def test_openai_and_anthropic_refusal_flags_are_explicit() -> None:
    for provider in ("openai", "anthropic"):
        result = _adapter(provider, _transport(provider, "refusal")).complete(_request())
        assert result.refusal is True, provider


def test_empty_content_is_empty_for_every_provider() -> None:
    for provider in sorted(ADAPTERS):
        result = _adapter(provider, _transport(provider, "empty_content")).complete(_request())
        assert result.raw_text == "", provider


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_schema_unsupported_400_maps_to_a_typed_error(provider: str) -> None:
    with pytest.raises(LLMSchemaUnsupported) as caught:
        _adapter(provider, _transport(provider, "schema_unsupported")).complete(_request())

    assert caught.value.code == "LLM_SCHEMA_UNSUPPORTED"


def test_openai_request_uses_strict_json_schema_and_never_leaks_the_key() -> None:
    captured: list[httpx.Request] = []

    _adapter("openai", _transport("openai", "valid", captured.append)).complete(_request())

    sent = json.loads(captured[0].content)
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["name"] == "analysis_decision"
    assert "pattern" not in json.dumps(sent["text"]["format"]["schema"])
    assert captured[0].headers["authorization"] == "Bearer sk-SENTINEL-DO-NOT-LEAK"


def test_anthropic_request_uses_output_config_and_api_key_header() -> None:
    captured: list[httpx.Request] = []

    _adapter("anthropic", _transport("anthropic", "valid", captured.append)).complete(_request())

    sent = json.loads(captured[0].content)
    assert sent["system"] == "system prompt"
    assert sent["messages"] == [{"role": "user", "content": "user prompt"}]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert captured[0].headers["x-api-key"] == "sk-SENTINEL-DO-NOT-LEAK"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"


def test_deepseek_request_uses_strict_tool_calling_without_defs() -> None:
    captured: list[httpx.Request] = []

    _adapter("deepseek", _transport("deepseek", "valid", captured.append)).complete(_request())

    sent = json.loads(captured[0].content)
    function = sent["tools"][0]["function"]
    assert sent["tools"][0]["type"] == "function"
    assert function["name"] == "analysis_decision"
    assert function["strict"] is True
    assert "$defs" not in json.dumps(function["parameters"])
    assert sent["tool_choice"] == {
        "type": "function",
        "function": {"name": "analysis_decision"},
    }


def test_deepseek_strict_mode_rejects_content_without_a_tool_call() -> None:
    """Strict tool calling must carry the decision in a tool call (D2)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "ds_content_only",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (PROVIDERS_DIR.parent / "analysis_valid.json").read_text(
                                encoding="utf-8"
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    adapter = DeepSeekAdapter(
        api_key="sk-SENTINEL-DO-NOT-LEAK",
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
    )

    result = adapter.complete(_request())

    assert result.raw_text == ""
    assert result.truncated is True
    assert validate_output(result.raw_text, role="ANALYSIS").status == "REJECTED"


def test_deepseek_a_genuine_tool_call_stays_valid() -> None:
    result = _adapter("deepseek", _transport("deepseek", "valid")).complete(_request())

    assert result.raw_text
    assert result.truncated is False
    assert validate_output(result.raw_text, role="ANALYSIS").status == "VALID"


def test_an_adapter_without_a_key_raises_auth_without_echoing_anything() -> None:
    for provider in sorted(ADAPTERS):
        adapter = ADAPTERS[provider](api_key=None, transport=_transport(provider, "valid"))
        with pytest.raises(LLMAuthError) as caught:
            adapter.complete(_request())
        assert caught.value.code == "LLM_AUTH_FAILED"
        assert "sk-" not in caught.value.message


def test_error_bodies_are_redacted_before_they_reach_the_message() -> None:
    sentinel = "sk-SENTINEL-DO-NOT-LEAK"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": f"invalid key {sentinel}"}},
            headers={"content-type": "application/json"},
        )

    adapter = OpenAIAdapter(
        api_key=sentinel,
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMAuthError) as caught:
        adapter.complete(_request())

    assert sentinel not in caught.value.message
    assert "[REDACTED]" in caught.value.message
