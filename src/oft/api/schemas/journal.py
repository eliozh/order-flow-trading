"""Trade-log journal response schemas (envelope-typed, minimal projection)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from oft.api.schemas.envelope import Envelope, EnvelopeMeta


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


__all__ = ["Envelope", "EnvelopeMeta", "TradeLogListData", "TradeLogSummary"]
