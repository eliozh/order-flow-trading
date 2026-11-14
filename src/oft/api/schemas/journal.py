"""Trade-log journal response schemas (envelope-typed, PnL/funding breakdown)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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

    @model_validator(mode="after")
    def _check_counts(self) -> JournalAvgR:
        if self.samples < 0 or self.excluded_null_r < 0:
            raise ValueError("average-R counts must be non-negative")
        if (self.value is not None) != (self.samples > 0):
            raise ValueError("average-R value must be present iff samples > 0")
        return self


class JournalTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gross_pnl_usd: str | None
    fees_usd: str | None
    funding_usd: str | None
    net_pnl_usd: str | None


class JournalStatsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["DEFINED", "UNDEFINED"]
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
    data_quality: Literal["COMPLETE", "PARTIAL", "MISSING"]

    @model_validator(mode="after")
    def _check_discriminants(self) -> JournalStatsData:
        if self.status == "DEFINED":
            if self.sample_size <= 0:
                raise ValueError("DEFINED statistics require a positive sample size")
            if self.wins + self.losses + self.ties != self.sample_size:
                raise ValueError("wins + losses + ties must equal sample_size")
            if self.win_rate is None or self.expectancy_usd is None:
                raise ValueError("DEFINED statistics require a win rate and expectancy")
            if self.avg_r.samples + self.avg_r.excluded_null_r != self.sample_size:
                raise ValueError("average-R samples must account for every eligible trade")
            return self
        if self.sample_size != 0 or self.wins or self.losses or self.ties:
            raise ValueError("UNDEFINED statistics require a zero sample and empty buckets")
        if self.win_rate is not None or self.expectancy_usd is not None:
            raise ValueError("UNDEFINED statistics must not fabricate a metric")
        if self.avg_r.value is not None or self.avg_r.samples or self.avg_r.excluded_null_r:
            raise ValueError("UNDEFINED statistics require an empty average-R result")
        totals = self.totals
        if any(
            value is not None
            for value in (
                totals.gross_pnl_usd,
                totals.fees_usd,
                totals.funding_usd,
                totals.net_pnl_usd,
            )
        ):
            raise ValueError("UNDEFINED statistics require empty totals")
        return self


class JournalExportData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["csv", "json"]
    path: str
    rows: int
    bytes: int
    neutralized_cells: int
    skipped_unreadable: int
    data_quality: str


class JournalComparisonSide(BaseModel):
    """The shared shape of the four signal-vs-outcome sides."""

    model_config = ConfigDict(extra="forbid")

    status: str
    reasons: list[str]


class JournalZoneData(JournalComparisonSide):
    status: Literal["INSIDE", "ABOVE_ZONE", "BELOW_ZONE", "N/A"]
    entry: str | None
    zone_low: str | None
    zone_high: str | None
    inside: bool | None
    distance_bps: float | None


class JournalSlippageData(JournalComparisonSide):
    status: Literal["OK", "PARTIAL", "N/A"]
    # The persisted snapshot values are rendered as exact decimal strings so the
    # response never carries a dynamic ``Any``.
    entry_avg_price: str | None
    reference_price: str | None
    reference_as_of: str | None
    bps: float | None
    formula: str


class JournalFirstTouchData(JournalComparisonSide):
    status: Literal["OK", "PARTIAL", "N/A"]
    kind: str | None
    at: str | None
    ambiguous: bool


class JournalDecisionData(JournalComparisonSide):
    status: Literal["OK", "PARTIAL", "N/A"]
    decision: str | None
    direction_agrees: bool | None
    matched: bool | None
    net_pnl_usd: str | None
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
    data_quality: Literal["COMPLETE", "PARTIAL", "MISSING"]


class JournalReviewData(BaseModel):
    """One advisory review (a persisted decision or a named missing state)."""

    model_config = ConfigDict(extra="forbid")

    trade_id: str
    decision_id: str | None = None
    status: Literal["FOUND", "MISSING", "PARTIAL"]
    advisory: bool
    rationale: str | None = None
    caveats: list[str] = []
    confidence: float | None = None
    validation_status: str | None = None
    provenance: dict[str, Any] | None = None
    reason: str | None = None
    idempotent_replay: bool = False


class JournalReviewRequest(BaseModel):
    """The idempotency payload for a review request (query values only)."""

    model_config = ConfigDict(extra="forbid")

    refresh: bool = False
    provider: str | None = None

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str | None) -> str | None:
        # The CLI derives its idempotency key from the stripped/lowered
        # provider; hash the same normalized value so case/whitespace variants
        # replay instead of conflicting.
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None


__all__ = [
    "Envelope",
    "EnvelopeMeta",
    "JournalAvgR",
    "JournalCompareData",
    "JournalComparisonSide",
    "JournalDecisionData",
    "JournalExclusions",
    "JournalExportData",
    "JournalFirstTouchData",
    "JournalPeriod",
    "JournalReviewData",
    "JournalReviewRequest",
    "JournalSlippageData",
    "JournalStatsData",
    "JournalTotals",
    "JournalZoneData",
    "TradeLogListData",
    "TradeLogSummary",
]
