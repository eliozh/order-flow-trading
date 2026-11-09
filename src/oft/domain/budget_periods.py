"""Canonical day/week/month bucketing for the budget ledger (D6, M4).

Every timestamp is converted to UTC before the calendar is derived. Both the
application ``budget_period`` helper and the store migration use this one
implementation so a non-UTC offset can never split a rollup across buckets.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime


def period_parts(instant: str) -> tuple[str, str, str]:
    """Return ``(day, iso_week, month)`` for an instant, bucketed in UTC."""
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    utc = parsed.astimezone(UTC)
    iso_year, iso_week, _ = utc.isocalendar()
    return (
        utc.strftime("%Y-%m-%d"),
        f"{iso_year}-W{iso_week:02d}",
        utc.strftime("%Y-%m"),
    )


def budget_record_id(call_id: str) -> str:
    """The deterministic budget-ledger id for one billed call."""
    return f"BUD-{hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:16].upper()}"


__all__ = ["budget_record_id", "period_parts"]
