"""OpenAI Responses API adapter (JSON schema mode, no SDK; D1/D2)."""

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

DEFAULT_BASE_URL = "https://api.openai.com"


class OpenAIAdapter(HttpProviderAdapter):
    provider = "openai"
    _path = "/v1/responses"

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
            "Authorization": f"Bearer {self._api_key or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_body(self, request: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": request.model,
            "input": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_output_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"{request.role.value.lower()}_decision",
                    "schema": schema,
                    "strict": True,
                }
            },
        }

    def _parse(
        self, body: dict[str, Any], response: httpx.Response, request: LLMRequest
    ) -> LLMResult:
        texts: list[str] = []
        refusal = False
        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text":
                        value = part.get("text")
                        texts.append(value if isinstance(value, str) else "")
                    elif part.get("type") == "refusal":
                        refusal = True
                        value = part.get("refusal")
                        texts.append(value if isinstance(value, str) else "")
        usage_body = body.get("usage")
        input_details = (
            usage_body.get("input_tokens_details") if isinstance(usage_body, dict) else {}
        )
        output_details = (
            usage_body.get("output_tokens_details") if isinstance(usage_body, dict) else {}
        )
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text="".join(texts),
            usage=usage_from_ints(
                input_tokens=int_field(usage_body, "input_tokens"),
                output_tokens=int_field(usage_body, "output_tokens"),
                cache_read_tokens=int_field(input_details, "cached_tokens"),
                cache_write_tokens=0,
                reasoning_tokens=int_field(output_details, "reasoning_tokens"),
            ),
            request_id=response_request_id(body, response.headers, request.request_id),
            refusal=refusal,
            truncated=body.get("status") == "incomplete",
            capabilities=self.capabilities(),
            usage_reported=usage_reported(usage_body, ("input_tokens", "output_tokens")),
        )


__all__ = ["DEFAULT_BASE_URL", "OpenAIAdapter"]
