"""Shared adapter helpers: provider schema translation and error redaction (D3).

The canonical provider-facing schema is never mutated. Translation strips only
keywords a provider cannot enforce and, for DeepSeek strict tool calling,
inlines ``$ref``/``$defs``. It never adds or removes a property, never changes
``required``, and never relaxes ``additionalProperties: false``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, cast

import httpx

from oft.ports.llm import (
    REDACTED,
    CircuitOpenError,
    CostEstimate,
    LLMAdapterError,
    LLMAuthError,
    LLMRateLimited,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMSchemaUnsupported,
    LLMTimeoutError,
    LLMTransportError,
    LLMUsage,
    ProbeOutcome,
    ProviderCapabilities,
    redact,
)

_STRIP_KEYWORDS = frozenset(
    {
        "pattern",
        "format",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "default",
    }
)
_OPENAI_KEEP = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "anyOf",
        "$defs",
        "$ref",
        "description",
        "title",
    }
)
_DEEPSEEK_KEEP = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "description",
    }
)
_DROP_ALWAYS = frozenset({"$schema", "$id"})


# -- schema translation ------------------------------------------------------


def _translate_keep(node: Any, keep: frozenset[str]) -> Any:
    if isinstance(node, list):
        return [_translate_keep(item, keep) for item in node]
    if not isinstance(node, dict):
        return node
    translated: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_ALWAYS or key in _STRIP_KEYWORDS:
            continue
        if key in {"properties", "$defs"} and isinstance(value, dict):
            translated[key] = {
                name: _translate_keep(schema, keep) for name, schema in value.items()
            }
        elif key in {"items", "anyOf"} or key in keep:
            translated[key] = _translate_keep(value, keep)
    return translated


def _resolve_ref(node: dict[str, Any], defs: dict[str, Any], depth: int) -> Any:
    ref = node.get("$ref")
    if not isinstance(ref, str) or depth > 32:
        return None
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        return None
    target = defs.get(ref[len(prefix) :])
    return copy.deepcopy(target) if target is not None else None


def _fold_nullable(branches: list[Any], resolve: Any) -> dict[str, Any] | None:
    if len(branches) != 2:
        return None
    first, second = branches
    if not isinstance(second, dict) or second != {"type": "null"}:
        return None
    resolved = resolve(first)
    if not isinstance(resolved, dict):
        return None
    extras = {
        key: value for key, value in resolved.items() if key not in {"type", "description", "title"}
    }
    if extras or "type" not in resolved:
        return None
    folded: dict[str, Any] = {"type": [resolved["type"], "null"]}
    if "description" in resolved:
        folded["description"] = resolved["description"]
    return folded


def _translate_deepseek(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if isinstance(node, list):
        return [_translate_deepseek(item, defs, depth) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        resolved = _resolve_ref(node, defs, depth)
        if resolved is None:
            raise LLMSchemaUnsupported(f"unresolvable schema $ref {node['$ref']!r}")
        return _translate_deepseek(resolved, defs, depth + 1)
    translated: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_ALWAYS or key == "$defs" or key in _STRIP_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, dict):
            translated[key] = {
                name: _translate_deepseek(schema, defs, depth) for name, schema in value.items()
            }
        elif key == "items":
            translated[key] = _translate_deepseek(value, defs, depth)
        elif key == "anyOf" and isinstance(value, list):
            folded = _fold_nullable(value, lambda item: _translate_deepseek(item, defs, depth))
            if folded is not None:
                translated.update(folded)
            else:
                translated[key] = [_translate_deepseek(item, defs, depth) for item in value]
        elif key in _DEEPSEEK_KEEP:
            translated[key] = _translate_deepseek(value, defs, depth)
    return translated


def translate_schema(canonical: dict[str, Any], provider: str) -> dict[str, Any]:
    """Translate the canonical schema for one provider without changing fields."""
    if provider == "deepseek":
        defs = canonical.get("$defs", {})
        return cast(
            dict[str, Any],
            _translate_deepseek(copy.deepcopy(canonical), defs if isinstance(defs, dict) else {}),
        )
    if provider in {"openai", "anthropic"}:
        # OpenAI strict json_schema and Anthropic structured outputs share the
        # same enforceable subset; both retain $defs/$ref and enums.
        return cast(dict[str, Any], _translate_keep(copy.deepcopy(canonical), _OPENAI_KEEP))
    raise ValueError(f"unknown schema provider {provider!r}")


# -- redaction ---------------------------------------------------------------
# ``redact`` and ``REDACTED`` live on the port (``oft.ports.llm``) so the
# application layer can sanitize provider text without importing an adapter;
# they are imported above and re-exported here for existing callers.


_SCHEMA_REJECTION_HINTS = (
    "schema",
    "additionalproperties",
    "output_config",
    "strict",
    "tool",
)


def is_schema_rejection(body_text: str) -> bool:
    lowered = body_text.lower()
    return any(hint in lowered for hint in _SCHEMA_REJECTION_HINTS)


def raise_for_provider_status(status: int, body_text: str, secrets: Iterable[str]) -> None:
    """Map a non-2xx provider response to a typed adapter error (D2).

    The provider body is always redacted before it reaches an exception message,
    a log, or a persisted row.
    """
    if 200 <= status < 300:
        return
    detail = redact(body_text, secrets)[:2000]
    if status in (401, 403):
        raise LLMAuthError(detail or "the provider rejected the API key", status=status)
    if status == 429:
        raise LLMRateLimited(detail or "the provider rate limited the request", status=status)
    if status == 408 or status >= 500:
        raise LLMTransportError(
            detail or "the provider is unavailable", status=status, retryable=True
        )
    if status == 400:
        if is_schema_rejection(body_text):
            raise LLMSchemaUnsupported(detail or "the provider rejected the schema", status=status)
        raise LLMTransportError(detail or "the provider rejected the request", status=status)
    raise LLMTransportError(detail or "the provider request failed", status=status)


def response_request_id(body: dict[str, Any], headers: Any, fallback: str) -> str:
    """Use the provider request id when present, else the request's own id."""
    value = body.get("id")
    if isinstance(value, str) and value:
        return value
    for name in ("x-request-id", "request-id"):
        header = headers.get(name)
        if isinstance(header, str) and header:
            return header
    return fallback


_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "string"}},
}
PROBE_MAX_OUTPUT_TOKENS = 64
# Hard cap on a provider response body before it is decoded or parsed (M19).
MAX_PROVIDER_BODY_BYTES = 4 * 1024 * 1024


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HttpProviderAdapter:
    """Shared HTTP plumbing; subclasses own only request/response translation."""

    provider = ""
    _path = ""
    _json_mode = False

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    # -- hooks -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request_body(self, request: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, body: dict[str, Any], lines: httpx.Response, request: LLMRequest) -> LLMResult:
        raise NotImplementedError

    def _degraded_clone(self) -> HttpProviderAdapter | None:
        return None

    def degraded_clone(self) -> HttpProviderAdapter | None:
        """A JSON-mode variant of this adapter, when the provider supports one (M12)."""
        return self._degraded_clone()

    # -- port ------------------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider,
            strict_schema=not self._json_mode,
            max_output_tokens=4096,
            notes=("degraded: JSON mode fallback" if self._json_mode else "strict schema"),
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="none/v1", unpriced=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        if not self._api_key:
            raise LLMAuthError(f"no {self.provider} API key is configured")
        try:
            canonical = json.loads(request.schema_json)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LLMTransportError("the canonical schema is not valid JSON") from exc
        schema = translate_schema(canonical, self.provider)
        payload = self._request_body(request, schema)
        started = time.perf_counter()
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    timeout=request.timeout_seconds,
                    # Ignore proxies/.netrc/SSL_CERT_FILE from the environment:
                    # only the operator-configured base URL may receive the key (S7).
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}{self._path}",
                    json=payload,
                    headers=self._headers(),
                ) as response,
            ):
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_PROVIDER_BODY_BYTES:
                        raise LLMTransportError(
                            f"the {self.provider} response exceeded the body cap"
                        )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"the {self.provider} request exceeded its timeout") from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"the {self.provider} request failed", retryable=True) from exc
        body_text = bytes(chunks).decode("utf-8", errors="replace")
        raise_for_provider_status(response.status_code, body_text, [self._api_key])
        try:
            parsed = json.loads(body_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMTransportError("the provider returned a non-JSON body") from exc
        if not isinstance(parsed, dict):
            raise LLMTransportError("the provider returned an unexpected body")
        result = self._parse(parsed, response, request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return replace(result, latency_ms=latency_ms)

    def probe(self, *, model: str, role: LLMRole = LLMRole.ANALYSIS) -> ProbeOutcome:
        request = LLMRequest(
            role=role,
            model=model,
            system_prompt="Respond with one JSON object matching the provided schema.",
            user_prompt='Return {"ok": "ok"}.',
            schema_json=json.dumps(_PROBE_SCHEMA, sort_keys=True),
            input_hash="capability-probe",
            prompt_id="capability_probe",
            prompt_version=1,
            prompt_hash="",
            max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
            timeout_seconds=45.0,
        )
        started = time.perf_counter()
        try:
            result = self.complete(request)
        except LLMSchemaUnsupported:
            return self._probe_degraded(request, started)
        except LLMAdapterError as exc:
            return ProbeOutcome(
                strict_schema=False,
                degraded=False,
                notes=redact(exc.message, [self._api_key or ""]),
                error_code=exc.code,
                max_output_tokens=self.capabilities().max_output_tokens,
                latency_ms=self._elapsed_ms(started),
            )
        return self._probe_outcome(result, started, strict_schema=True, degraded=False)

    def _elapsed_ms(self, started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _probe_outcome(
        self,
        result: LLMResult,
        started: float,
        *,
        strict_schema: bool,
        degraded: bool,
    ) -> ProbeOutcome:
        """Validate a 200 probe body; a refusal/empty/garbage body is FAILED (M7)."""
        max_output_tokens = self.capabilities().max_output_tokens
        latency_ms = self._elapsed_ms(started)
        usage = getattr(result, "usage", None)
        usage_is_reported = bool(getattr(result, "usage_reported", False))
        raw_output_hash = _hash_text(result.raw_text)

        def failure(code: str, note: str) -> ProbeOutcome:
            return ProbeOutcome(
                strict_schema=False,
                degraded=degraded,
                notes=note,
                error_code=code,
                max_output_tokens=max_output_tokens,
                latency_ms=latency_ms,
                usage=usage,
                raw_output_hash=raw_output_hash,
                request_id=result.request_id,
                usage_reported=usage_is_reported,
            )

        if result.refusal:
            return failure("LLM_REFUSED", "the model refused the probe request")
        if result.truncated or not result.raw_text.strip():
            return failure("LLM_OUTPUT_TRUNCATED", "the probe output was truncated or empty")
        try:
            parsed = json.loads(result.raw_text)
        except (json.JSONDecodeError, ValueError):
            return failure("LLM_SCHEMA_INVALID", "the probe output was not valid JSON")
        if not (
            isinstance(parsed, dict) and set(parsed) == {"ok"} and isinstance(parsed["ok"], str)
        ):
            return failure("LLM_SCHEMA_INVALID", "the probe output did not match {'ok': <string>}")
        return ProbeOutcome(
            strict_schema=strict_schema,
            degraded=degraded,
            notes="strict schema accepted" if strict_schema else "degraded: JSON mode fallback",
            error_code=None,
            max_output_tokens=max_output_tokens,
            latency_ms=latency_ms,
            usage=usage,
            raw_output_hash=raw_output_hash,
            request_id=result.request_id,
            usage_reported=usage_is_reported,
        )

    def _probe_degraded(self, request: LLMRequest, started: float) -> ProbeOutcome:
        fallback = self.degraded_clone()
        max_output_tokens = self.capabilities().max_output_tokens
        if fallback is None:
            return ProbeOutcome(
                strict_schema=False,
                degraded=False,
                notes="strict schemas are not supported",
                error_code="LLM_SCHEMA_UNSUPPORTED",
                max_output_tokens=max_output_tokens,
                latency_ms=self._elapsed_ms(started),
            )
        try:
            result = fallback.complete(request)
        except LLMAdapterError as exc:
            return ProbeOutcome(
                strict_schema=False,
                degraded=True,
                notes=f"degraded: JSON mode fallback failed ({exc.code})",
                error_code=exc.code,
                max_output_tokens=max_output_tokens,
                latency_ms=self._elapsed_ms(started),
            )
        return self._probe_outcome(result, started, strict_schema=False, degraded=True)


def usage_from_ints(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> LLMUsage:
    return LLMUsage(
        input_tokens=max(int(input_tokens), 0),
        output_tokens=max(int(output_tokens), 0),
        cache_read_tokens=max(int(cache_read_tokens), 0),
        cache_write_tokens=max(int(cache_write_tokens), 0),
        reasoning_tokens=max(int(reasoning_tokens), 0),
    )


def _int_like(value: Any) -> bool:
    """A non-negative integer or an integral float; bools and negatives are not."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and value.is_integer() and value >= 0


# Provider token counts are untrusted; clamp so cost math cannot overflow.
_MAX_TOKEN_COUNT = 2**31 - 1


def int_field(container: Any, name: str) -> int:
    """Read an integer token count, accepting non-negative integral floats (M10)."""
    if not isinstance(container, dict):
        return 0
    value = container.get(name)
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return min(value, _MAX_TOKEN_COUNT) if value >= 0 else 0
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return min(int(value), _MAX_TOKEN_COUNT)
    return 0


def usage_reported(container: Any, names: Iterable[str]) -> bool:
    """Whether a provider usage object has *every* expected integral field (M10).

    ``{}``, a missing field, a negative value, or a non-integral value is not a
    usable usage object; such a call must be recorded unpriced, never priced $0.
    """
    if not isinstance(container, dict):
        return False
    return all(name in container and _int_like(container[name]) for name in names)


__all__ = [
    "CircuitOpenError",
    "HttpProviderAdapter",
    "LLMAdapterError",
    "LLMAuthError",
    "LLMRateLimited",
    "LLMSchemaUnsupported",
    "LLMTimeoutError",
    "LLMTransportError",
    "MAX_PROVIDER_BODY_BYTES",
    "PROBE_MAX_OUTPUT_TOKENS",
    "REDACTED",
    "int_field",
    "is_schema_rejection",
    "raise_for_provider_status",
    "redact",
    "response_request_id",
    "translate_schema",
    "usage_from_ints",
    "usage_reported",
]
