"""Clock port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """Supplies the current time; never call ``datetime.now`` in domain code."""

    def now(self) -> datetime: ...
