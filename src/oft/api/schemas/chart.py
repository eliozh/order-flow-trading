"""Response schema for the chart-panel payload (slice 39).

The route returns this shape; the contract test validates live responses with
``ChartPayload.model_validate`` so an additive field or a renamed key cannot
drift silently.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ChartPayload(BaseModel):
    """One resolved chart: a frozen report's bars/metrics or an honest MISSING."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    symbol: str
    native_period: str
    requested_period: str
    status: Literal["AVAILABLE", "MISSING"]
    available_periods: list[str]
    source_report_id: str | None
    generated_at: str | None
    bars: list[dict[str, Any]]
    metrics: dict[str, Any] | None
    gaps: list[dict[str, Any]]
    data_quality: str
    quality_reasons: list[str]
    reason: str | None
