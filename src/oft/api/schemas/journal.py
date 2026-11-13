"""Trade-log journal response schemas (envelope-typed, minimal projection)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class TradeLogSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_id: str
    position_id: str
    symbol: str
    side: str
    opened_at: str
    closed_at: str
    gross_pnl_usd: str
    fees_usd: str
    funding_usd: str
    net_pnl_usd: str
    return_pct: float


class TradeLogListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TradeLogSummary]
    total: int


class EnvelopeMeta(BaseModel):
    request_id: str
    generated_at: str
    duration_ms: int
    provenance: str
    data_quality: str | None
    warnings: list[Any]
    pagination: dict[str, Any] | None
    idempotent_replay: bool


class Envelope[T](BaseModel):
    ok: bool
    data: T
    error: Any = None
    meta: EnvelopeMeta


__all__ = ["Envelope", "EnvelopeMeta", "TradeLogListData", "TradeLogSummary"]
