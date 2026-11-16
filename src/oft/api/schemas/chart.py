"""Response schema for the chart-panel payload (slice 39).

The route returns this shape; the contract test validates live responses with
``ChartPayload.model_validate`` so an additive field or a renamed key cannot
drift silently.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ChartZone(BaseModel):
    """One server-derived opportunity zone and its trigger price.

    ``low``/``high``/``trigger_price`` are the stored price strings verbatim, so
    the client positions the band at exactly the recorded prices. The state is
    the stored opportunity state; the client renders it as a visible word and a
    dash pattern, never as color alone.
    """

    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    side: str
    state: str
    low: str
    high: str
    trigger_price: str
    label: str


class ChartMarker(BaseModel):
    """One server-derived entry/exit/stop/target marker at an exact price."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["entry", "exit", "stop", "target"]
    price: str
    label: str
    position_id: str | None


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
    zones: list[ChartZone]
    markers: list[ChartMarker]
    #: True when a linked read or the marker cap clipped the overlay lists.
    #: Always False on a MISSING payload (nothing is drawn).
    overlays_truncated: bool
    data_quality: str
    quality_reasons: list[str]
    reason: str | None
