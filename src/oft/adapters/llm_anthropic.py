"""Anthropic Messages API adapter (structured outputs, no SDK; D1/D2)."""

from __future__ import annotations

from typing import Any

import httpx

from oft.adapters.llm_common import (
    HttpProviderAdapter,
    int_field,
    response_request_id,
    usage_from_ints,
    usage_reported,
)
from oft.ports.llm import LLMRequest, LLMResult

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(HttpProviderAdapter):
    provider = "anthropic"
    _path = "/v1/messages"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_body(self, request: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }

    def _parse(
        self, body: dict[str, Any], response: httpx.Response, request: LLMRequest
    ) -> LLMResult:
        texts: list[str] = []
        tool_use = False
        content = body.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    value = block.get("text")
                    texts.append(value if isinstance(value, str) else "")
                elif block.get("type") == "tool_use":
                    # We never send tools; an unexpected tool_use block makes the
                    # output unusable and is treated as truncated, never repaired.
                    tool_use = True
        stop_reason = body.get("stop_reason")
        usage_body = body.get("usage")
        raw_text = "" if tool_use else "".join(texts)
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text=raw_text,
            usage=usage_from_ints(
                input_tokens=int_field(usage_body, "input_tokens"),
                output_tokens=int_field(usage_body, "output_tokens"),
                cache_read_tokens=int_field(usage_body, "cache_read_input_tokens"),
                cache_write_tokens=int_field(usage_body, "cache_creation_input_tokens"),
                reasoning_tokens=0,
            ),
            request_id=response_request_id(body, response.headers, request.request_id),
            refusal=stop_reason == "refusal",
            truncated=stop_reason == "max_tokens" or tool_use,
            capabilities=self.capabilities(),
            usage_reported=usage_reported(usage_body, ("input_tokens", "output_tokens")),
        )


__all__ = ["ANTHROPIC_VERSION", "AnthropicAdapter", "DEFAULT_BASE_URL"]
