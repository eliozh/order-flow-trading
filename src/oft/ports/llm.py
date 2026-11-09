"""Provider-neutral LLM port (ADR-0005).

No provider SDK object, model client, credential, or execution import crosses
this boundary. The application depends on this protocol only; the stub and the
real adapters implement it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# -- provider error contract -------------------------------------------------
#
# These live on the port so the application layer can handle provider failures
# without importing an adapter. Adapters raise them; application code catches
# them; ``oft.adapters.llm_common`` re-exports them for existing callers.

REDACTED = "[REDACTED]"
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9._-]+")

# A provider controls its own response id; redact and bound it before it is
# persisted or returned so a hostile provider cannot echo a key into SQLite/API.
MAX_PROVIDER_ID_CHARS = 200
_TOKEN_MAX = 2**31 - 1


class LLMAdapterError(Exception):
    """Base class for a provider adapter failure with a stable error code."""

    code = "UPSTREAM_UNAVAILABLE"
    retryable = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class LLMAuthError(LLMAdapterError):
    code = "LLM_AUTH_FAILED"
    retryable = False


class LLMRateLimited(LLMAdapterError):
    code = "LLM_RATE_LIMITED"
    retryable = True


class LLMTransportError(LLMAdapterError):
    code = "UPSTREAM_UNAVAILABLE"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message, status=status)
        self.retryable = retryable


class LLMTimeoutError(LLMAdapterError):
    code = "LLM_TIMEOUT"
    retryable = True


class LLMSchemaUnsupported(LLMAdapterError):
    code = "LLM_SCHEMA_UNSUPPORTED"
    retryable = False


class CircuitOpenError(LLMAdapterError):
    """The per-provider circuit breaker is open; fail fast without a request."""

    code = "LLM_RATE_LIMITED"
    retryable = False


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace configured secret values and Bearer/sk- token shapes."""
    cleaned = text
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        if secret.strip():
            cleaned = cleaned.replace(secret, REDACTED)
    cleaned = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", cleaned)
    cleaned = _SECRET_KEY_PATTERN.sub(REDACTED, cleaned)
    return cleaned


def sanitize_provider_id(value: object, secrets: Iterable[str] = ()) -> str:
    """Redact and bound a provider-controlled id before persistence/return (S6/S12).

    Only printable characters survive and the result is capped so a hostile or
    compromised provider cannot smuggle a secret or unbounded text into SQLite
    or an API response.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    cleaned = redact(text, secrets)
    printable = "".join(character for character in cleaned if character.isprintable())
    return printable[:MAX_PROVIDER_ID_CHARS]


class LLMRole(StrEnum):
    ANALYSIS = "ANALYSIS"
    ENTRY_RECHECK = "ENTRY_RECHECK"
    MANAGEMENT = "MANAGEMENT"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    strict_schema: bool
    max_output_tokens: int = 4096
    notes: str = ""


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )

    def to_contract(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMRequest:
    role: LLMRole
    model: str
    system_prompt: str
    user_prompt: str
    schema_json: str
    input_hash: str
    prompt_id: str
    prompt_version: int
    prompt_hash: str
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_seconds: float = 120.0
    request_id: str = ""
    repair_of: str | None = None
    system_prompt_id: str = ""
    system_prompt_version: int = 1
    system_prompt_hash: str = ""


@dataclass(frozen=True)
class LLMResult:
    provider: str
    model: str
    raw_text: str
    parsed: Any = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    request_id: str = ""
    refusal: bool = False
    truncated: bool = False
    capabilities: ProviderCapabilities | None = None
    # ``False`` when the provider response carried no usage object (or a value
    # that had to be coerced); the budget ledger then records the call unpriced
    # and appends a local ``LLM_USAGE_MISSING`` audit event.
    usage_reported: bool = True


@dataclass(frozen=True)
class CostEstimate:
    amount_usd: str
    price_version: str
    unpriced: bool


@dataclass(frozen=True)
class ProbeOutcome:
    """One capability-probe result; failures are recorded, never raised."""

    strict_schema: bool
    degraded: bool = False
    notes: str = ""
    error_code: str | None = None
    max_output_tokens: int = 4096
    latency_ms: int = 0
    # Provenance used to record the billable probe as an ``llm_calls`` row and
    # budget row (M8): the provider usage metadata and the output identity.
    usage: LLMUsage | None = None
    raw_output_hash: str = ""
    request_id: str = ""
    # True only when the adapter received a usage object with every expected
    # integral field; a probe without usable usage is recorded unpriced.
    usage_reported: bool = False
    # False when the probe never reached the provider (circuit open, no
    # concurrency slot); such an outcome is not a billable call (silent-3 N1).
    request_made: bool = True


class LLMPort(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, request: LLMRequest) -> LLMResult: ...

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate: ...


class ProbeableLLMPort(LLMPort, Protocol):
    """An adapter that can learn whether the provider enforces strict schemas."""

    def probe(self, *, model: str, role: LLMRole = LLMRole.ANALYSIS) -> ProbeOutcome: ...
