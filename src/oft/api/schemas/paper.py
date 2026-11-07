"""Paper account and kill-switch request schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    token: str | None = Field(default=None, max_length=32)
