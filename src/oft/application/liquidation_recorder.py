"""Application facade for the live-only liquidation accumulator.

The deterministic aggregation lives in :mod:`oft.domain.metrics.liquidations`.
This module is the application boundary used by ingestion and persistence:
it exposes the recorder, the recorded/live-shaped JSONL loader, and the
sha256 input hash recorded with each published liquidation part.

Live-only rule: coverage starts at deployment. The public ``forceOrder`` feed
pushes only the largest liquidation per symbol per second, and no historical
USD-M liquidation backfill exists.
"""

from __future__ import annotations

from pathlib import Path

from oft.domain.metrics.liquidations import (
    LIMITATIONS,
    Coverage,
    LiquidationBucket,
    LiquidationEvent,
    LiquidationRecorder,
    LiquidationResult,
    LiquidationSide,
    compute_liquidations,
    liquidation_input_hash,
    load_liquidation_events,
)
from oft.ports.clock import ClockPort

__all__ = [
    "LIMITATIONS",
    "Coverage",
    "LiquidationBucket",
    "LiquidationEvent",
    "LiquidationRecorder",
    "LiquidationResult",
    "LiquidationSide",
    "compute_liquidations",
    "liquidation_input_hash",
    "load_liquidation_events",
    "recorded_liquidation_recorder",
]


def recorded_liquidation_recorder(
    path: Path,
    *,
    clock: ClockPort,
    bucket_seconds: int = 60,
) -> LiquidationRecorder:
    """Open a recorder over a recorded/live-shaped fixture and start coverage.

    The fixture contains public ``forceOrder``-shaped events captured from a
    live feed; it is not and cannot be historical USD-M liquidation data.
    """
    return LiquidationRecorder.from_jsonl(path, clock=clock, bucket_seconds=bucket_seconds)
