"""Provider response bodies are bounded before they are decoded (M19)."""

from __future__ import annotations

import httpx
import pytest

from oft.adapters.llm_common import MAX_PROVIDER_BODY_BYTES, LLMTransportError
from oft.adapters.llm_openai import OpenAIAdapter
from oft.application.llm_validation import schema_text
from oft.ports.llm import LLMRequest, LLMRole


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
    )


def test_an_oversized_response_body_is_rejected_before_parsing() -> None:
    # Valid JSON at the byte level: without the cap this body would parse and
    # return successfully, so the assertion below really pins the cap (S9).
    oversized = b'{"id": "' + b"a" * MAX_PROVIDER_BODY_BYTES + b'"}'
    adapter = OpenAIAdapter(
        api_key="sk-test",
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=oversized)),
    )

    with pytest.raises(LLMTransportError) as caught:
        adapter.complete(_request())

    assert "body cap" in caught.value.message
