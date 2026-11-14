"""Deterministic management classification: risk direction, stops, adds, exits."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.oms import PositionSide
from oft.domain.risk import (
    ManagementAction,
    ManagementClassification,
    StopChange,
    add_risk_quantity,
    classify_management_intent,
    management_is_exit,
    management_requires_risk_verdict,
)


def _classify(
    action: str,
    *,
    side: PositionSide = PositionSide.LONG,
    current: str | None = "69000.00",
    new: str | None = None,
    remove_stop: bool = False,
):
    return classify_management_intent(
        action=action,
        position_side=side,
        current_stop=None if current is None else Decimal(current),
        new_stop_price=None if new is None else Decimal(new),
        remove_stop=remove_stop,
    )


def test_hold_without_changes_does_not_increase_risk() -> None:
    result = _classify("HOLD")

    assert result.risk_increasing is False
    assert result.stop_change is StopChange.NONE
    assert result.reasons == ()


def test_partial_and_full_close_are_risk_reducing() -> None:
    partial = _classify("PARTIAL_CLOSE")
    full = _classify("FULL_CLOSE")

    assert partial.risk_increasing is False
    assert full.risk_increasing is False
    assert "reduce-only close" in " ".join(partial.reasons)
    assert "reduce-only close" in " ".join(full.reasons)


def test_add_always_increases_risk() -> None:
    for action in ("ADD", "add", " Add "):
        result = _classify(action)
        assert result.risk_increasing is True
        assert "add increases exposure" in " ".join(result.reasons)


def test_long_stop_tightening_reduces_and_loosening_increases() -> None:
    tighter = _classify("HOLD", new="69500.00")
    looser = _classify("HOLD", new="68000.00")

    assert tighter.stop_change is StopChange.TIGHTEN
    assert tighter.risk_increasing is False
    assert looser.stop_change is StopChange.LOOSEN
    assert looser.risk_increasing is True


def test_short_stop_tightening_reduces_and_loosening_increases() -> None:
    tighter = _classify("HOLD", side=PositionSide.SHORT, new="68500.00")
    looser = _classify("HOLD", side=PositionSide.SHORT, new="70000.00")

    assert tighter.stop_change is StopChange.TIGHTEN
    assert tighter.risk_increasing is False
    assert looser.stop_change is StopChange.LOOSEN
    assert looser.risk_increasing is True


def test_removing_a_stop_is_risk_increasing_and_setting_one_reduces() -> None:
    removed = _classify("HOLD", remove_stop=True)
    unchanged = _classify("HOLD", current=None, new=None)
    set_stop = _classify("HOLD", current=None, new="69000.00")

    assert removed.stop_change is StopChange.REMOVE
    assert removed.risk_increasing is True
    assert unchanged.stop_change is StopChange.NONE
    assert unchanged.risk_increasing is False
    assert set_stop.stop_change is StopChange.SET
    assert set_stop.risk_increasing is False


def test_a_null_stop_means_no_change_not_removal() -> None:
    result = _classify("PARTIAL_CLOSE")

    assert result.stop_change is StopChange.NONE
    assert result.risk_increasing is False


def test_an_unchanged_stop_is_neither_tightened_nor_loosened() -> None:
    result = _classify("HOLD", new="69000.00")

    assert result.stop_change is StopChange.NONE
    assert result.risk_increasing is False


def test_a_full_close_ignores_a_stop_move() -> None:
    result = _classify("FULL_CLOSE", new="60000.00")

    assert result.stop_change is StopChange.NONE
    assert result.risk_increasing is False


def test_unknown_action_or_side_is_rejected() -> None:
    with pytest.raises(ValueError):
        _classify("SCALE_IN")
    with pytest.raises(ValueError):
        classify_management_intent(
            action="HOLD",
            position_side="FLAT",  # type: ignore[arg-type]
            current_stop=Decimal("69000.00"),
            new_stop_price=None,
        )


def test_management_helpers_name_the_verdict_and_exit_boundaries() -> None:
    assert management_requires_risk_verdict("ADD") is True
    assert management_requires_risk_verdict(" add ") is True
    assert management_requires_risk_verdict("PARTIAL_CLOSE") is False
    assert management_is_exit("PARTIAL_CLOSE") is True
    assert management_is_exit("full_close") is True
    assert management_is_exit("ADD") is False
    assert management_is_exit("HOLD") is False


def test_add_risk_quantity_turns_a_percentage_into_the_requested_size() -> None:
    quantity = add_risk_quantity(
        equity=Decimal("10000"),
        add_risk_pct=0.5,
        mark_price=Decimal("70000"),
        stop_price=Decimal("69000"),
    )

    # 0.5% of 10,000 = 50 USD risk; a 1,000 stop distance gives 0.05.
    assert quantity == Decimal("0.05")


@pytest.mark.parametrize(
    ("equity", "pct", "mark", "stop"),
    (
        (Decimal("10000"), 0.0, Decimal("70000"), Decimal("69000")),
        (Decimal("10000"), -0.5, Decimal("70000"), Decimal("69000")),
        (Decimal("10000"), 100.5, Decimal("70000"), Decimal("69000")),
        (Decimal("10000"), float("nan"), Decimal("70000"), Decimal("69000")),
        (Decimal("0"), 0.5, Decimal("70000"), Decimal("69000")),
        (Decimal("10000"), 0.5, Decimal("0"), Decimal("69000")),
        (Decimal("10000"), 0.5, Decimal("70000"), Decimal("0")),
        (Decimal("10000"), 0.5, Decimal("69000"), Decimal("69000")),
    ),
)
def test_add_risk_quantity_rejects_unusable_inputs(
    equity: Decimal, pct: float, mark: Decimal, stop: Decimal
) -> None:
    assert (
        add_risk_quantity(equity=equity, add_risk_pct=pct, mark_price=mark, stop_price=stop) is None
    )


def test_every_action_classification_is_stable_for_a_repeated_input() -> None:
    for action in ManagementAction:
        first = _classify(action.value, side=PositionSide.LONG, current="69000", new="69500")
        second = _classify(action.value, side=PositionSide.LONG, current="69000", new="69500")
        assert first == second


def test_a_classification_cannot_disagree_with_the_stop_direction() -> None:
    """M11: LOOSEN/REMOVE implies risk_increasing, so the state is unrepresentable."""
    with pytest.raises(ValueError):
        ManagementClassification(
            risk_increasing=False,
            stop_change=StopChange.LOOSEN,
            reasons=(),
        )
    with pytest.raises(ValueError):
        ManagementClassification(
            risk_increasing=False,
            stop_change=StopChange.REMOVE,
            reasons=(),
        )
    allowed = ManagementClassification(
        risk_increasing=True,
        stop_change=StopChange.LOOSEN,
        reasons=("loosening the stop increases risk",),
    )
    assert allowed.risk_increasing is True
