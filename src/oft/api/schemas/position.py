"""Position read/monitor response schemas (envelope-typed)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from oft.api.schemas.envelope import Envelope, EnvelopeMeta


class PositionListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]
    total: int
    counts: dict[str, int] | None = None


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


__all__ = ["Envelope", "EnvelopeMeta", "PositionListData", "PositionMonitorData"]
