"""Trigger evaluate/accept/reject request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TriggerEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(min_length=1, max_length=64)
    price: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=32)
    as_of: datetime | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)


class TriggerRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class TriggerConditionResult(BaseModel):
    condition_id: str
    type: str
    met: bool
    observed_value: str | None
    threshold: str | None
    evaluated_at: str
    data_quality: str
    evaluable: bool
    note: str


class TriggerEvaluationData(BaseModel):
    met: bool
    stale: bool
    snapshot_id: str
    snapshot_hash: str
    as_of: str
    data_quality: str
    reasons: list[str]
    condition_results: list[TriggerConditionResult]


class TriggerEvaluateData(BaseModel):
    opportunity_id: str
    triggered: bool
    trigger: dict[str, Any] | None
    evaluation: TriggerEvaluationData


class TriggerAcceptData(BaseModel):
    status: str
    decision_id: str
    risk_verdict: dict[str, Any]
    order: dict[str, Any] | None
    fill: dict[str, Any] | None
    position: dict[str, Any] | None
    account: dict[str, Any] | None
