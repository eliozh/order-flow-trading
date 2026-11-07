"""Opportunity ledger schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OpportunityListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["OPEN", "TRIGGERED", "EXPIRED", "INVALIDATED"] | None = None
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
