"""Open-interest delta with explicit, documented irregular resampling.

Regular 5-minute samples are measured directly. Irregular TradFi timestamps
are resampled onto the target grid by carrying the last observed value forward;
every synthesized point is marked ``estimated=True`` and the result is stamped
``IRREGULAR_RESAMPLED`` + ``ESTIMATED`` with a reason. Interpolation is never
presented as a measured value.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from oft.domain.data_quality import DataQuality
from oft.domain.metrics.types import MetricQuality, decimal_str
from oft.domain.time import utc_timestamp

DEFAULT_RESAMPLE_INTERVAL = timedelta(minutes=5)
OpenInterestUnit = Literal["BASE", "QUOTE", "CONTRACTS"]
Sampling = Literal["REGULAR", "IRREGULAR_RESAMPLED"]
_VALID_UNITS = frozenset({"BASE", "QUOTE", "CONTRACTS"})


@dataclass(frozen=True)
class OpenInterestSample:
    """One source open-interest observation."""

    ts: datetime
    open_interest: Decimal
    open_interest_value: Decimal
    long_short_ratio: Decimal | None = None
    taker_ratio: Decimal | None = None


@dataclass(frozen=True)
class OpenInterestPoint:
    """One output point; ``estimated`` is true only for resampled values."""

    ts: datetime
    open_interest: Decimal
    open_interest_value: Decimal
    long_short_ratio: Decimal | None
    taker_ratio: Decimal | None
    estimated: bool

    def to_contract(self) -> dict[str, object]:
        return {
            "ts": utc_timestamp(self.ts),
            "open_interest": decimal_str(self.open_interest),
            "open_interest_value": decimal_str(self.open_interest_value),
            "long_short_ratio": self.long_short_ratio,
            "taker_ratio": self.taker_ratio,
        }


@dataclass(frozen=True)
class OpenInterestResult:
    """An open-interest section plus its sampling/quality stamp."""

    unit: OpenInterestUnit
    sampling: Sampling
    points: tuple[OpenInterestPoint, ...]
    delta_over_window: Decimal
    last: OpenInterestPoint | None
    quality: MetricQuality

    def to_contract(self) -> dict[str, object]:
        if self.last is None:
            raise ValueError("open-interest result has no measured point to render")
        return {
            "unit": self.unit,
            "sampling": self.sampling,
            "points": [point.to_contract() for point in self.points],
            "delta_over_window": decimal_str(self.delta_over_window),
            "last": {
                "ts": utc_timestamp(self.last.ts),
                "open_interest": decimal_str(self.last.open_interest),
                "open_interest_value": decimal_str(self.last.open_interest_value),
            },
        }


def _validate_sample(sample: OpenInterestSample) -> None:
    if sample.ts.tzinfo is None:
        raise ValueError("open-interest timestamps must be timezone-aware")
    if not sample.open_interest.is_finite() or sample.open_interest < 0:
        raise ValueError("open_interest must be a finite non-negative decimal")
    if not sample.open_interest_value.is_finite() or sample.open_interest_value < 0:
        raise ValueError("open_interest_value must be a finite non-negative decimal")
    for name, value in (
        ("long_short_ratio", sample.long_short_ratio),
        ("taker_ratio", sample.taker_ratio),
    ):
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError(f"{name} must be a finite non-negative decimal")


def _point(
    sample: OpenInterestSample,
    *,
    estimated: bool,
    ts: datetime | None = None,
) -> OpenInterestPoint:
    return OpenInterestPoint(
        ts=sample.ts if ts is None else ts,
        open_interest=sample.open_interest,
        open_interest_value=sample.open_interest_value,
        long_short_ratio=sample.long_short_ratio,
        taker_ratio=sample.taker_ratio,
        estimated=estimated,
    )


def _resampled_points(
    samples: Sequence[OpenInterestSample], interval: timedelta
) -> tuple[OpenInterestPoint, ...]:
    first = samples[0]
    last = samples[-1]
    points: dict[datetime, OpenInterestPoint] = {
        sample.ts: _point(sample, estimated=False) for sample in samples
    }
    sample_times = [sample.ts for sample in samples]
    grid = first.ts
    while grid < last.ts:
        if grid not in points:
            # Carry the most recent observation forward; it is an estimate at
            # this timestamp, never a measured value.
            source = samples[bisect_right(sample_times, grid) - 1]
            points[grid] = _point(source, estimated=True, ts=grid)
        grid += interval
    return tuple(points[ts] for ts in sorted(points))


def compute_open_interest(
    samples: Sequence[OpenInterestSample],
    *,
    window_start: datetime,
    window_end: datetime,
    unit: OpenInterestUnit = "BASE",
    target_interval: timedelta = DEFAULT_RESAMPLE_INTERVAL,
) -> OpenInterestResult:
    """Compute ``OI_end - OI_start`` and mark resampled points as estimated."""
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if unit not in _VALID_UNITS:
        raise ValueError("unit must be BASE, QUOTE, or CONTRACTS")
    if not isinstance(target_interval, timedelta) or target_interval <= timedelta(0):
        raise ValueError("target_interval must be a positive timedelta")

    for sample in samples:
        _validate_sample(sample)
    selected = [sample for sample in samples if window_start <= sample.ts < window_end]
    selected.sort(key=lambda sample: sample.ts)
    if len({sample.ts for sample in selected}) != len(selected):
        raise ValueError("duplicate open-interest timestamps")

    if not selected:
        return OpenInterestResult(
            unit=unit,
            sampling="REGULAR",
            points=(),
            delta_over_window=Decimal("0"),
            last=None,
            quality=MetricQuality(
                data_quality=DataQuality.MISSING,
                quality_reasons=("no open-interest samples in the requested window",),
            ),
        )

    gaps = {selected[index + 1].ts - selected[index].ts for index in range(len(selected) - 1)}
    regular = all(gap == target_interval for gap in gaps)
    first = selected[0]
    last = selected[-1]
    delta = last.open_interest - first.open_interest
    spans_window = (
        first.ts == window_start
        and last.ts + target_interval >= window_end
        and last.ts < window_end
    )
    if regular:
        points = tuple(_point(sample, estimated=False) for sample in selected)
        if spans_window:
            quality = MetricQuality.complete()
        else:
            quality = MetricQuality.partial(
                ("open_interest samples do not span the requested window",)
            )
        sampling: Sampling = "REGULAR"
    else:
        points = _resampled_points(selected, target_interval)
        minutes = int(target_interval.total_seconds() // 60)
        reasons = [
            "open_interest source timestamps are irregular; "
            f"resampled to {minutes}m and ESTIMATED (not measured)"
        ]
        if not spans_window:
            reasons.append("open_interest samples do not span the requested window")
        quality = MetricQuality(
            data_quality=DataQuality.ESTIMATED,
            quality_reasons=tuple(reasons),
        )
        sampling = "IRREGULAR_RESAMPLED"

    return OpenInterestResult(
        unit=unit,
        sampling=sampling,
        points=points,
        delta_over_window=delta,
        last=_point(last, estimated=False),
        quality=quality,
    )
