"""UTC day/week/month bucketing is shared and offset-independent (M4)."""

from __future__ import annotations

from oft.application.budget_service import budget_period
from oft.domain.budget_periods import period_parts


def test_a_late_local_evening_rolls_the_day_forward_in_utc() -> None:
    parts = budget_period("2026-11-08T23:30:00-05:00")

    # 04:30Z on 2026-11-09 (ISO week 46).
    assert parts == {"day": "2026-11-09", "week": "2026-W46", "month": "2026-11"}


def test_the_last_local_day_of_october_rolls_into_november_in_utc() -> None:
    parts = budget_period("2026-10-31T23:30:00-05:00")

    assert parts["day"] == "2026-11-01"
    assert parts["month"] == "2026-11"


def test_iso_year_boundary_uses_the_iso_week_year() -> None:
    # 2027-01-01Z is Friday of ISO week 2026-W53.
    parts = budget_period("2027-01-01T00:30:00Z")

    assert parts["week"] == "2026-W53"


def test_the_store_helper_matches_the_application_helper() -> None:
    from oft.adapters.sqlite_store import _period_parts

    instant = "2026-11-08T23:30:00-05:00"

    assert _period_parts(instant) == period_parts(instant)
