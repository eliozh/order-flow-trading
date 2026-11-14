"""Trade-log journal response schemas (envelope-typed, PnL/funding breakdown)."""

from __future__ import annotations

from typing import Any

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
    r_multiple: float | None = None
    mae_usd: str = "0"
    mfe_usd: str = "0"
    outcome_tags: list[str] = []
    data_quality: dict[str, Any] | None = None


class TradeLogListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TradeLogSummary]
    total: int


class JournalPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    start: str
    end: str
    basis: str


class JournalExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_positions: int
    partial_trades: int
    unreadable_trades: int
    outside_period: int


class JournalAvgR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None
    samples: int
    excluded_null_r: int


class JournalTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gross_pnl_usd: str | None
    fees_usd: str | None
    funding_usd: str | None
    net_pnl_usd: str | None


class JournalStatsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    reason: str | None
    period: JournalPeriod | None
    sample_size: int
    denominator: int
    exclusions: JournalExclusions
    definitions: dict[str, str]
    wins: int
    losses: int
    ties: int
    win_rate: float | None
    expectancy_usd: str | None
    avg_r: JournalAvgR
    totals: JournalTotals
    data_quality: str


class JournalExportData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str
    path: str
    rows: int
    bytes: int
    neutralized: int
    skipped_unreadable: int
    data_quality: str


class JournalZoneData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    entry: str | None
    zone_low: str | None
    zone_high: str | None
    inside: bool | None
    distance_bps: float | None
    reasons: list[str]


class JournalSlippageData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    # The persisted snapshot values are passed through verbatim (string, number,
    # or null), so these three fields are genuinely dynamic.
    entry_avg_price: Any = None
    reference_price: Any = None
    reference_as_of: Any = None
    bps: float | None
    formula: str
    reasons: list[str]


class JournalFirstTouchData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None
    at: str | None
    ambiguous: bool
    status: str
    reasons: list[str]


class JournalDecisionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    decision: str | None
    direction_agrees: bool | None
    matched: bool | None
    net_pnl_usd: str | None
    reasons: list[str]
    decision_id: str | None = None


class JournalCompareData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_id: str | None
    position_id: str | None
    symbol: str | None
    side: str | None
    opened_at: str | None
    closed_at: str | None
    net_pnl_usd: str | None
    zone: JournalZoneData
    slippage: JournalSlippageData
    first_touch: JournalFirstTouchData
    decision: JournalDecisionData
    reasons: list[str]
    data_quality: str


__all__ = [
    "Envelope",
    "EnvelopeMeta",
    "JournalAvgR",
    "JournalCompareData",
    "JournalDecisionData",
    "JournalExclusions",
    "JournalExportData",
    "JournalFirstTouchData",
    "JournalPeriod",
    "JournalSlippageData",
    "JournalStatsData",
    "JournalTotals",
    "JournalZoneData",
    "TradeLogListData",
    "TradeLogSummary",
]
