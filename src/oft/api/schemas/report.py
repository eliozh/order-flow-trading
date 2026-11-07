"""Request/response schemas for the metrics-only scan and report surface."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScanCreateRequest(BaseModel):
    """Manual scan request; omitting ``symbol`` scans every starred symbol."""

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2,24}$")
    period: Literal["1m", "5m", "15m", "1h", "4h", "8h", "1d"] = "4h"
    source: Literal["backfill", "live"] = "backfill"
    metrics_only: bool = True
    provider: Literal["stub"] | None = None
    start: date | None = None
    end: date | None = None
    fixture_path: str | None = Field(default=None, min_length=1)


class FixtureScanCreateRequest(BaseModel):
    """Legacy fixture-backed scan request (walking-skeleton debug surface)."""

    model_config = ConfigDict(extra="forbid")

    fixture_path: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    period: Literal["1m", "5m", "15m", "1h", "4h", "8h", "1d"]
    metrics_only: bool = True


class ReportSummary(BaseModel):
    """A compact report row for list surfaces."""

    report_id: str
    scan_id: str
    symbol: str
    timeframe: str
    generated_at: str
    data_quality: str
    llm_decision_id: str | None = None
