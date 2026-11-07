"""Replay clock: deterministic event time, never the wall clock.

The same clock type is used by the harness and by tests. Advancing is
monotonic: a replay that tries to move backwards is a bug (out-of-order
events), not something to paper over.
"""

from __future__ import annotations

from datetime import UTC, datetime


class ReplayClock:
    """A clock that only moves forward to the next emitted event's time."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("replay clock start must be timezone-aware")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, instant: datetime) -> datetime:
        """Move to ``instant``; raises when time would move backwards."""
        if instant.tzinfo is None:
            raise ValueError("replay clock instants must be timezone-aware")
        resolved = instant.astimezone(UTC)
        if resolved < self._now:
            raise ValueError(
                f"replay clock cannot move backwards: {resolved.isoformat()} "
                f"< {self._now.isoformat()}"
            )
        self._now = resolved
        return self._now
