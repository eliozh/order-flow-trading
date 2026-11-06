"""Deterministic funding rate, next funding time, interval, and history.

A missing funding period is reported as ``MISSING`` with a reason. It is never
inserted into the history as a zero rate, because a zero would look like a
measured funding round.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from oft.domain.data_quality import DataQuality
from oft.domain.metrics.types import MetricQuality, decimal_str
from oft.domain.time import utc_timestamp

_VALID_INTERVALS = frozenset({4, 8})
FundingInterval = Literal[4, 8]


@dataclass(frozen=True)
class FundingSample:
    """One measured funding round plus its mark/index context."""

    ts: datetime
    rate: Decimal
    mark_price: Decimal
    index_price: Decimal
    interval_hours: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("funding timestamps must be timezone-aware")
        if isinstance(self.interval_hours, bool) or self.interval_hours not in _VALID_INTERVALS:
            raise ValueError("interval_hours must be 4 or 8")
        if not self.rate.is_finite():
            raise ValueError("funding rate must be a finite decimal")
        if not self.mark_price.is_finite() or self.mark_price <= 0:
            raise ValueError("mark_price must be a positive finite decimal")
        if not self.index_price.is_finite() or self.index_price <= 0:
            raise ValueError("index_price must be a positive finite decimal")


@dataclass(frozen=True)
class FundingHistoryPoint:
    """One funding round in the rendered history."""

    ts: datetime
    rate: Decimal
    interval_hours: int

    def to_contract(self) -> dict[str, object]:
        return {
            "ts": utc_timestamp(self.ts),
            "rate": decimal_str(self.rate),
            "interval_hours": self.interval_hours,
        }


@dataclass(frozen=True)
class FundingResult:
    """The funding section plus its quality (MISSING for absent periods)."""

    current_rate: Decimal | None
    next_funding_time: datetime | None
    interval_hours: FundingInterval | None
    mark_price: Decimal | None
    index_price: Decimal | None
    history: tuple[FundingHistoryPoint, ...]
    missing_periods: tuple[datetime, ...]
    quality: MetricQuality

    def to_contract(self) -> dict[str, object]:
        values = (
            self.current_rate,
            self.next_funding_time,
            self.interval_hours,
            self.mark_price,
            self.index_price,
        )
        if any(value is None for value in values):
            raise ValueError("funding result has no measured period to render")
        assert self.current_rate is not None
        assert self.next_funding_time is not None
        assert self.interval_hours is not None
        assert self.mark_price is not None
        assert self.index_price is not None
        return {
            "current_rate": decimal_str(self.current_rate),
            "next_funding_time": utc_timestamp(self.next_funding_time),
            "interval_hours": self.interval_hours,
            "mark_price": decimal_str(self.mark_price),
            "index_price": decimal_str(self.index_price),
            "history": [point.to_contract() for point in self.history],
        }


def _missing_periods(samples: Sequence[FundingSample], interval: timedelta) -> tuple[datetime, ...]:
    actual = {sample.ts for sample in samples}
    expected: list[datetime] = []
    cursor = samples[0].ts
    last = samples[-1].ts
    while cursor <= last:
        if cursor not in actual:
            expected.append(cursor)
        cursor += interval
    return tuple(expected)


def compute_funding(
    samples: Sequence[FundingSample],
    *,
    as_of: datetime,
) -> FundingResult:
    """Summarize measured funding rounds at or before ``as_of``.

    ``interval_hours`` is validated per sample and must be consistent. Missing
    expected rounds are listed in ``missing_periods`` and stamped ``MISSING``;
    they are not written into ``history`` as zeroes.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    selected = [sample for sample in samples if sample.ts <= as_of]
    selected.sort(key=lambda sample: sample.ts)
    if len({sample.ts for sample in selected}) != len(selected):
        raise ValueError("duplicate funding timestamps")
    if not selected:
        return FundingResult(
            current_rate=None,
            next_funding_time=None,
            interval_hours=None,
            mark_price=None,
            index_price=None,
            history=(),
            missing_periods=(),
            quality=MetricQuality(
                data_quality=DataQuality.MISSING,
                quality_reasons=("no funding data at or before the requested time",),
            ),
        )

    intervals = {sample.interval_hours for sample in selected}
    if len(intervals) != 1:
        raise ValueError("funding interval must be consistent across samples")
    interval_hours = selected[0].interval_hours
    if interval_hours not in _VALID_INTERVALS:
        raise ValueError("interval_hours must be 4 or 8")
    interval = timedelta(hours=interval_hours)
    last = selected[-1]
    # Trailing gap: scheduled rounds after the last sample but at/before as_of
    # are unmeasured too and must not be reported as a complete section.
    next_funding = last.ts + interval
    missing_periods = list(_missing_periods(selected, interval))
    while next_funding <= as_of:
        missing_periods.append(next_funding)
        next_funding += interval
    missing = tuple(missing_periods)
    if missing:
        quality = MetricQuality(
            data_quality=DataQuality.MISSING,
            quality_reasons=tuple(
                f"funding period {utc_timestamp(period)} is MISSING; not treated as zero"
                for period in missing
            ),
        )
    else:
        quality = MetricQuality.complete()

    return FundingResult(
        current_rate=last.rate,
        next_funding_time=next_funding,
        interval_hours=interval_hours,  # type: ignore[arg-type]
        mark_price=last.mark_price,
        index_price=last.index_price,
        history=tuple(
            FundingHistoryPoint(
                ts=sample.ts,
                rate=sample.rate,
                interval_hours=sample.interval_hours,
            )
            for sample in selected
        ),
        missing_periods=missing,
        quality=quality,
    )
