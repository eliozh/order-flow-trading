"""LLM-band request/response schemas (walking-skeleton subset)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LLMState(BaseModel):
    """The additive ``llm`` band attached to a report."""

    model_config = ConfigDict(extra="allow")

    configured: bool
    provider: str | None = None
    model: str | None = None
    validation_status: str | None = None
    decision_type: str | None = None
    confidence: float | None = None
    narrative: str | None = None
    rationale: str | None = None
    opportunity_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    cost: dict[str, Any] | None = None
