"""Provider-neutral LLM port (ADR-0005).

No provider SDK object, model client, credential, or execution import crosses
this boundary. The application depends on this protocol only; the stub and the
real adapters implement it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


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


@dataclass(frozen=True)
class CostEstimate:
    amount_usd: str
    price_version: str
    unpriced: bool


class LLMPort(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, request: LLMRequest) -> LLMResult: ...

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate: ...
