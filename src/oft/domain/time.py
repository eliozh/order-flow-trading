"""Canonical timestamp formatting for contracts."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_timestamp(value: datetime) -> str:
    """Format a timezone-aware datetime as ISO-8601 UTC with milliseconds and ``Z``."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    utc_value = value.astimezone(UTC)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_value.microsecond // 1000:03d}Z"
