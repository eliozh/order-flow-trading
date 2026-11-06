"""Load metrics/funding samples and compute OI + funding sections.

The fixture is a small synthetic sample shaped like the public Binance USD-M
``metrics`` and funding/mark-price endpoints. XAUUSDT rows deliberately carry
irregular timestamps so the resampling path is exercised; the OI result is
then stamped ``IRREGULAR_RESAMPLED`` + ``ESTIMATED`` with a reason.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from oft.domain.metrics.funding import FundingResult, FundingSample, compute_funding
from oft.domain.metrics.open_interest import (
    DEFAULT_RESAMPLE_INTERVAL,
    OpenInterestResult,
    OpenInterestSample,
    OpenInterestUnit,
    compute_open_interest,
)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
_REQUIRED_COLUMNS = frozenset(
    {
        "symbol",
        "record_type",
        "ts",
        "open_interest",
        "open_interest_value",
        "long_short_ratio",
        "taker_ratio",
        "funding_rate",
        "mark_price",
        "index_price",
        "funding_interval_hours",
    }
)


@dataclass(frozen=True)
class MetricsFixtureData:
    """Normalized OI and funding samples from one metrics fixture."""

    open_interest: tuple[OpenInterestSample, ...]
    funding: tuple[FundingSample, ...]


def _timestamp(value: str, *, field: str, line: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"row {line}: {field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"row {line}: {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _decimal(value: str, *, field: str, line: int) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"row {line}: {field} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"row {line}: {field} must be finite")
    return parsed


def _optional_decimal(value: str | None, *, field: str, line: int) -> Decimal | None:
    if value is None or not value.strip():
        return None
    return _decimal(value, field=field, line=line)


def load_metrics_fixture(path: Path) -> MetricsFixtureData:
    """Parse OPEN_INTEREST and FUNDING rows from a metrics sample CSV."""
    if not path.is_file():
        raise ValueError(f"metrics fixture not found: {path}")
    open_interest: list[OpenInterestSample] = []
    funding: list[FundingSample] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or ())
            missing = _REQUIRED_COLUMNS - columns
            if missing:
                raise ValueError(f"missing columns: {', '.join(sorted(missing))}")
            for line, row in enumerate(reader, start=2):
                symbol = (row.get("symbol") or "").strip()
                if _SYMBOL_PATTERN.fullmatch(symbol) is None:
                    raise ValueError(f"row {line}: symbol is invalid")
                record_type = (row.get("record_type") or "").strip().upper()
                ts = _timestamp(row.get("ts") or "", field="ts", line=line)
                if record_type == "OPEN_INTEREST":
                    open_interest.append(
                        OpenInterestSample(
                            ts=ts,
                            open_interest=_decimal(
                                row.get("open_interest") or "",
                                field="open_interest",
                                line=line,
                            ),
                            open_interest_value=_decimal(
                                row.get("open_interest_value") or "",
                                field="open_interest_value",
                                line=line,
                            ),
                            long_short_ratio=_optional_decimal(
                                row.get("long_short_ratio"),
                                field="long_short_ratio",
                                line=line,
                            ),
                            taker_ratio=_optional_decimal(
                                row.get("taker_ratio"),
                                field="taker_ratio",
                                line=line,
                            ),
                        )
                    )
                elif record_type == "FUNDING":
                    interval_raw = (row.get("funding_interval_hours") or "").strip()
                    if not interval_raw:
                        raise ValueError(f"row {line}: funding_interval_hours is required")
                    try:
                        interval = int(interval_raw)
                    except ValueError as exc:
                        raise ValueError(
                            f"row {line}: funding_interval_hours must be an integer"
                        ) from exc
                    funding.append(
                        FundingSample(
                            ts=ts,
                            rate=_decimal(
                                row.get("funding_rate") or "",
                                field="funding_rate",
                                line=line,
                            ),
                            mark_price=_decimal(
                                row.get("mark_price") or "",
                                field="mark_price",
                                line=line,
                            ),
                            index_price=_decimal(
                                row.get("index_price") or "",
                                field="index_price",
                                line=line,
                            ),
                            interval_hours=interval,
                        )
                    )
                else:
                    raise ValueError(f"row {line}: unsupported record_type {record_type!r}")
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"invalid metrics fixture {path.name}: {exc}") from exc
    return MetricsFixtureData(
        open_interest=tuple(open_interest),
        funding=tuple(funding),
    )


class OiFundingService:
    """Compute OI and funding sections from normalized fixture samples."""

    def __init__(
        self,
        *,
        data: MetricsFixtureData,
        resample_interval: timedelta = DEFAULT_RESAMPLE_INTERVAL,
    ) -> None:
        self._data = data
        self._resample_interval = resample_interval

    @classmethod
    def from_csv(
        cls,
        path: Path,
        *,
        resample_interval: timedelta = DEFAULT_RESAMPLE_INTERVAL,
    ) -> OiFundingService:
        return cls(data=load_metrics_fixture(path), resample_interval=resample_interval)

    def open_interest(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        unit: OpenInterestUnit = "BASE",
    ) -> OpenInterestResult:
        return compute_open_interest(
            self._data.open_interest,
            window_start=window_start,
            window_end=window_end,
            unit=unit,
            target_interval=self._resample_interval,
        )

    def funding(self, *, as_of: datetime) -> FundingResult:
        return compute_funding(self._data.funding, as_of=as_of)
