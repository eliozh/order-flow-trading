"""Concrete clock implementations; time is always injected."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


class SystemClock:
    """Wall clock in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """Deterministic clock for tests and replay."""

    instant: datetime

    def now(self) -> datetime:
        return self.instant
