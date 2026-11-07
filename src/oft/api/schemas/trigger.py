"""Trigger evaluate/accept/reject request schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TriggerEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(min_length=1, max_length=64)
    price: str | None = Field(default=None, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
    as_of: datetime | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)


class TriggerRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
