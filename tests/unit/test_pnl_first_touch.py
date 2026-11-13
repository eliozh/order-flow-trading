"""First-touch evaluation: stop/target order and the conservative AMBIGUOUS_BAR case."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.pnl import STOP_FIRST, TARGET_FIRST, FirstTouch, first_touch


def _bar(low: str, high: str, bar_end: str = "2026-11-14T12:00:00.000Z") -> dict[str, object]:
    return {"low": low, "high": high, "bar_end": bar_end}


def test_long_stop_touch_is_found_first() -> None:
    bars = (_bar("68900", "70100"),)
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=Decimal("72000"))
    assert touch == FirstTouch(STOP_FIRST, False, "2026-11-14T12:00:00.000Z")


def test_long_target_touch_is_found_when_the_stop_is_not_hit() -> None:
    bars = (_bar("69500", "72500"),)
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=Decimal("72000"))
    assert touch == FirstTouch(TARGET_FIRST, False, "2026-11-14T12:00:00.000Z")


def test_a_bar_containing_both_stop_and_target_is_ambiguous_and_stop_first() -> None:
    bars = (_bar("68500", "72500"),)
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=Decimal("72000"))
    assert touch.kind == STOP_FIRST
    assert touch.ambiguous is True


def test_short_stop_touch_is_symmetric() -> None:
    bars = (_bar("69500", "71500"),)
    touch = first_touch("SHORT", bars, stop_price=Decimal("71000"), target_price=Decimal("68000"))
    assert touch == FirstTouch(STOP_FIRST, False, "2026-11-14T12:00:00.000Z")


def test_short_target_touch_is_symmetric() -> None:
    bars = (_bar("67500", "70500"),)
    touch = first_touch("SHORT", bars, stop_price=Decimal("71000"), target_price=Decimal("68000"))
    assert touch == FirstTouch(TARGET_FIRST, False, "2026-11-14T12:00:00.000Z")


def test_no_touch_returns_none() -> None:
    bars = (_bar("69500", "70500"),)
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=Decimal("72000"))
    assert touch == FirstTouch(None, False, None)


def test_a_stop_only_evaluation_never_reports_a_target() -> None:
    bars = (_bar("69500", "72500"),)
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=None)
    assert touch.kind is None


def test_a_later_bar_is_used_when_the_first_does_not_touch() -> None:
    bars = (
        _bar("69500", "70500", "2026-11-14T12:00:00.000Z"),
        _bar("68900", "70100", "2026-11-14T16:00:00.000Z"),
    )
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=None)
    assert touch.at == "2026-11-14T16:00:00.000Z"


def test_an_unusable_bar_is_skipped() -> None:
    bars = ({"low": None, "high": "1", "bar_end": "t"}, _bar("68900", "70100", "t2"))
    touch = first_touch("LONG", bars, stop_price=Decimal("69000"), target_price=None)
    assert touch.at == "t2"


def test_no_stop_and_no_target_is_invalid() -> None:
    with pytest.raises(ValueError):
        first_touch("LONG", (), stop_price=None, target_price=None)


def test_an_invalid_side_is_rejected() -> None:
    with pytest.raises(ValueError):
        first_touch("FLAT", (), stop_price=Decimal("1"), target_price=None)
