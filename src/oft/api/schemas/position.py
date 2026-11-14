"""Position read/monitor response schemas (envelope-typed)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oft.api.schemas.envelope import Envelope, EnvelopeMeta


class PositionListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]
    total: int
    counts: dict[str, int] | None = None


class PositionManageRequest(BaseModel):
    """One management request; the LLM action itself is never caller-supplied."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, min_length=1, max_length=32)


class PositionManageRefusal(BaseModel):
    """One typed refusal attached to an executed management result."""

    model_config = ConfigDict(extra="forbid")

    code: str
    field: str
    message: str


class PositionManageData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str
    decision_id: str
    action: str
    status: str
    executed: bool
    reason_code: str
    reason: str | None = None
    llm: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    trade_log: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    refusals: list[PositionManageRefusal] = Field(default_factory=list)
    idempotent_replay: bool = False
    # Additive: the due conditions that authorized an automated window decision.
    due_conditions: list[str] = Field(default_factory=list)


class PositionMonitorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str
    status: str
    reason_code: str
    reason: str | None = None
    action: str | None = None
    order: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    trade_log: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    idempotent_replay: bool = False
    funding: dict[str, Any] | None = None


__all__ = [
    "Envelope",
    "EnvelopeMeta",
    "PositionListData",
    "PositionManageData",
    "PositionManageRefusal",
    "PositionManageRequest",
    "PositionMonitorData",
]
