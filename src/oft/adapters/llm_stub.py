"""Deterministic fixture-backed LLM provider (``provider="stub"``, ``model="fixture"``).

The stub never opens a socket and never needs a key. It returns a committed
fixture by role so the paper loop is reproducible offline (ADR-0011).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from oft.config import DEFAULT_LLM_FIXTURES_DIR
from oft.ports.llm import (
    CostEstimate,
    LLMRequest,
    LLMResult,
    LLMRole,
    ProbeOutcome,
    ProviderCapabilities,
)

_DEFAULT_FIXTURES: dict[LLMRole, str] = {
    LLMRole.ANALYSIS: "opportunity_valid.json",
    LLMRole.ENTRY_RECHECK: "entry_recheck_enter_long.json",
    LLMRole.MANAGEMENT: "management_hold.json",
    LLMRole.REVIEW: "analysis_valid.json",
}


class LLMStub:
    """Fixture-backed provider with deterministic provenance."""

    def __init__(
        self,
        *,
        fixtures_dir: Path | None = None,
        overrides: Mapping[LLMRole, str] | None = None,
    ) -> None:
        self._fixtures_dir = fixtures_dir or DEFAULT_LLM_FIXTURES_DIR
        self._overrides = dict(overrides or {})

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="stub",
            strict_schema=True,
            max_output_tokens=4096,
            notes="deterministic fixture provider; no network or credential",
        )

    def _fixture_name(self, role: LLMRole) -> str:
        return self._overrides.get(role, _DEFAULT_FIXTURES[role])

    def complete(self, request: LLMRequest) -> LLMResult:
        path = self._fixtures_dir / self._fixture_name(request.role)
        raw_text = path.read_text(encoding="utf-8")
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text=raw_text,
            parsed=None,
            latency_ms=0,
            request_id=request.request_id or f"stub-{request.input_hash[:12]}",
            refusal=False,
            truncated=False,
            capabilities=self.capabilities(),
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)

    def probe(self, *, model: str, role: LLMRole = LLMRole.ANALYSIS) -> ProbeOutcome:
        """The fixture provider always enforces the canonical schema."""
        return ProbeOutcome(
            strict_schema=True,
            degraded=False,
            notes="deterministic fixture provider; no network or credential",
        )
