"""DeepSeek adapter: strict tool calling on ``/beta`` with a JSON-mode fallback.

Strict tool calling is beta and may change; the capability probe learns whether
it is available and the adapter can run in prompt-steered JSON mode. JSON mode
can return empty content, which is always rejected as truncated (D1/D2).
"""

from __future__ import annotations

import json
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

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekAdapter(HttpProviderAdapter):
    provider = "deepseek"
    _path = "/beta/chat/completions"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        json_mode: bool = False,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, transport=transport)
        self._json_mode = json_mode

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_body(self, request: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ]
        if self._json_mode:
            schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            return {
                "model": request.model,
                "max_tokens": request.max_output_tokens,
                "temperature": request.temperature,
                "messages": [
                    {
                        "role": "system",
                        "content": request.system_prompt,
                    },
                    {
                        "role": "user",
                        "content": (
                            f"{request.user_prompt}\n\nReturn only JSON matching this "
                            f"schema:\n{schema_text}"
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
            }
        name = f"{request.role.value.lower()}_decision"
        return {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "Return the decision object.",
                        "parameters": schema,
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": name}},
        }

    def degraded_clone(self) -> HttpProviderAdapter | None:
        return DeepSeekAdapter(
            api_key=self._api_key,
            base_url=self._base_url,
            transport=self._transport,
            json_mode=True,
        )

    def _parse(
        self, body: dict[str, Any], response: httpx.Response, request: LLMRequest
    ) -> LLMResult:
        choices = body.get("choices")
        choice: dict[str, Any] = {}
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}
        tool_calls = message.get("tool_calls")
        raw_text = ""
        has_tool_call = False
        if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[0], dict):
            function = tool_calls[0].get("function")
            if isinstance(function, dict):
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    raw_text = arguments
                    has_tool_call = True
        if not has_tool_call:
            content = message.get("content")
            # Strict tool calling must carry the decision in a tool call; prose
            # content is not a valid decision. JSON mode is the only path where
            # ``message.content`` carries the raw decision text.
            raw_text = content if (self._json_mode and isinstance(content, str)) else ""
        finish_reason = choice.get("finish_reason")
        usage_body = body.get("usage")
        output_details = (
            usage_body.get("completion_tokens_details") if isinstance(usage_body, dict) else {}
        )
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text=raw_text,
            usage=usage_from_ints(
                input_tokens=int_field(usage_body, "prompt_tokens"),
                output_tokens=int_field(usage_body, "completion_tokens"),
                cache_read_tokens=int_field(usage_body, "prompt_cache_hit_tokens"),
                cache_write_tokens=0,
                reasoning_tokens=int_field(output_details, "reasoning_tokens"),
            ),
            request_id=response_request_id(body, response.headers, request.request_id),
            refusal=False,
            truncated=(finish_reason == "length") or (not has_tool_call and not raw_text.strip()),
            capabilities=self.capabilities(),
            usage_reported=usage_reported(usage_body, ("prompt_tokens", "completion_tokens")),
        )


__all__ = ["DEFAULT_BASE_URL", "DeepSeekAdapter"]
