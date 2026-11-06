"""Gap detection: expected daily files/bars versus what is actually present."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from oft.config import Settings
from oft.domain.data_quality import (
    DataGap,
    DataQuality,
    GapSeverity,
    QualityResult,
    quality_for,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort

MISSING_FILE_REASON = "ARCHIVE_FILE_MISSING"
INSUFFICIENT_BARS_REASON = "INSUFFICIENT_BARS"
GAP_SOURCE = "BACKFILL"


@dataclass(frozen=True)
class GapDetectionResult:
    """Quality stamp for one expected window plus the gaps that caused it."""

    symbol: str
    data_type: str
    start: str
    end: str
    expected_days: int
    present_days: int
    quality: QualityResult


def expected_dates(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        return ()
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _gap(
    *,
    symbol: str,
    data_type: str,
    day: date,
    reason: str,
    severity: GapSeverity,
    detected_at: str,
    run_id: str | None,
    suffix: str = "",
) -> DataGap:
    return DataGap(
        gap_id=f"GAP-{symbol}-{data_type}-{day.isoformat()}{suffix}",
        symbol=symbol,
        data_type=data_type,
        start=f"{day.isoformat()}T00:00:00.000Z",
        end=f"{day.isoformat()}T23:59:59.999Z",
        reason=reason,
        severity=severity,
        detected_at=detected_at,
        source=GAP_SOURCE,
        run_id=run_id,
    )


def detect_daily_gaps(
    *,
    symbol: str,
    data_type: str,
    start: date,
    end: date,
    present_dates: Iterable[date | str],
    clock: ClockPort,
    expected_bars_per_day: int | None = None,
    bars_by_date: Mapping[date | str, int] | None = None,
    run_id: str | None = None,
) -> GapDetectionResult:
    """Compare expected daily files (and optional bar counts) with reality."""
    expected = expected_dates(start, end)
    present = {_as_date(value) for value in present_dates}
    detected_at = utc_timestamp(clock.now())
    gaps: list[DataGap] = []

    for day in expected:
        if day not in present:
            gaps.append(
                _gap(
                    symbol=symbol,
                    data_type=data_type,
                    day=day,
                    reason=MISSING_FILE_REASON,
                    severity=GapSeverity.CRITICAL,
                    detected_at=detected_at,
                    run_id=run_id,
                )
            )

    if expected_bars_per_day is not None and bars_by_date is not None:
        counts = {_as_date(key): value for key, value in bars_by_date.items()}
        for day in expected:
            if day not in present:
                continue
            rows = counts.get(day, 0)
            if rows < expected_bars_per_day:
                gaps.append(
                    _gap(
                        symbol=symbol,
                        data_type=data_type,
                        day=day,
                        reason=INSUFFICIENT_BARS_REASON,
                        severity=GapSeverity.WARNING,
                        detected_at=detected_at,
                        run_id=run_id,
                        suffix="-bars",
                    )
                )

    ordered = tuple(sorted(gaps, key=lambda gap: (gap.start, gap.gap_id)))
    return GapDetectionResult(
        symbol=symbol,
        data_type=data_type,
        start=start.isoformat(),
        end=end.isoformat(),
        expected_days=len(expected),
        present_days=len(present & set(expected)),
        quality=quality_for(ordered),
    )


def _partition_dates(settings: Settings, symbol: str) -> set[date]:
    root = settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"
    if not root.is_dir():
        return set()
    present: set[date] = set()
    for partition in root.glob("date=*"):
        if not any(partition.glob("*.parquet")):
            continue
        try:
            present.add(date.fromisoformat(partition.name.split("=", 1)[1]))
        except ValueError:
            continue
    return present


def _row_counts(settings: Settings, symbol: str) -> dict[date | str, int]:
    import pyarrow.parquet as parquet

    counts: dict[date | str, int] = {}
    root = settings.data_dir / "market" / "um" / "agg_trades" / f"symbol={symbol}"
    for partition in root.glob("date=*"):
        parts = [path for path in partition.glob("*.parquet") if path.is_file()]
        if not parts:
            continue
        try:
            day = date.fromisoformat(partition.name.split("=", 1)[1])
        except ValueError:
            continue
        counts[day] = sum(parquet.read_table(part).num_rows for part in parts)
    return counts


def detect_backfill_gaps(
    settings: Settings,
    store: StorePort,
    *,
    symbol: str,
    data_type: str,
    start: date,
    end: date,
    clock: ClockPort,
    expected_bars_per_day: int | None = None,
    bars_by_date: Mapping[date | str, int] | None = None,
    run_id: str | None = None,
) -> GapDetectionResult:
    """Detect gaps from published Parquet partitions and persist them.

    Gap IDs are deterministic, so re-running detection updates the same rows
    instead of creating duplicates.
    """
    present = _partition_dates(settings, symbol)
    if expected_bars_per_day is not None and bars_by_date is None:
        bars_by_date = _row_counts(settings, symbol)
    result = detect_daily_gaps(
        symbol=symbol,
        data_type=data_type,
        start=start,
        end=end,
        present_dates=present,
        clock=clock,
        expected_bars_per_day=expected_bars_per_day,
        bars_by_date=bars_by_date,
        run_id=run_id,
    )
    for gap in result.quality.gaps:
        store.upsert_data_gap(gap)
    return result


__all__ = [
    "GapDetectionResult",
    "detect_backfill_gaps",
    "detect_daily_gaps",
    "expected_dates",
]


def quality_status(result: GapDetectionResult) -> DataQuality:
    """Small helper for callers that only need the stamp."""
    return result.quality.status
