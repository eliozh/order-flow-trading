"""Data-quality and gap domain models shared by backfill and metrics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DataQuality(StrEnum):
    """The ADR-0008 quality stamp attached to every metric input."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    ESTIMATED = "ESTIMATED"


class GapSeverity(StrEnum):
    """How strongly a gap affects downstream calculations."""

    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class DataGap:
    """One observed gap in an expected daily series."""

    gap_id: str
    symbol: str
    data_type: str
    start: str
    end: str
    reason: str
    severity: GapSeverity
    detected_at: str
    source: str
    resolved_at: str | None = None
    resolution: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class QualityResult:
    """The quality status plus human-readable reasons and structured gaps."""

    status: DataQuality
    reasons: tuple[str, ...]
    gaps: tuple[DataGap, ...]


def quality_for(gaps: tuple[DataGap, ...]) -> QualityResult:
    """Derive the quality stamp from the gaps that were found."""
    if not gaps:
        return QualityResult(status=DataQuality.COMPLETE, reasons=(), gaps=())
    reasons = tuple(f"{gap.reason.lower().replace('_', ' ')} {gap.start[:10]}" for gap in gaps)
    return QualityResult(status=DataQuality.PARTIAL, reasons=reasons, gaps=gaps)
