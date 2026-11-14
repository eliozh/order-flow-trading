"""Semantic validation and deterministic sizing for management recommendations."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from oft.domain.management import (
    ManagementSizingError,
    plan_partial_close,
    round_down_to_step,
    validate_position_management,
)
from oft.domain.oms import Position, PositionSide


def _position(*, side: PositionSide = PositionSide.LONG, size: str = "0.08") -> Position:
    return Position(
        position_id="POS-1",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=side,
        state="OPEN",
        size=Decimal(size),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70010"),
        notional_usd=Decimal("5600"),
        stop_price=Decimal("69000"),
        target_prices=(Decimal("72000"),),
        leverage=2.0,
        margin_usd=Decimal("2800"),
        risk_amount_usd=Decimal("80"),
        risk_pct=0.8,
        fees_usd=Decimal("2"),
        opened_at="2026-10-08T00:00:00.000Z",
        updated_at="2026-10-08T00:00:00.000Z",
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "HOLD",
        "close_fraction": None,
        "add_risk_pct": None,
        "new_stop_price": None,
        "new_targets": [],
        "rationale": "hold",
        "urgency": "LOW",
    }
    payload.update(overrides)
    return payload


def _codes(issues: tuple) -> set[str]:
    return {issue.code for issue in issues}


def test_a_clean_hold_is_valid() -> None:
    assert validate_position_management(_payload(), _position(), mark_price=Decimal("70010")) == ()


def test_hold_carrying_any_change_is_rejected() -> None:
    issues = validate_position_management(
        _payload(
            close_fraction=0.5,
            add_risk_pct=0.4,
            new_stop_price="69500",
            new_targets=["73000"],
        ),
        _position(),
        mark_price=Decimal("70010"),
    )

    assert {issue.field for issue in issues} >= {
        "position_management.close_fraction",
        "position_management.add_risk_pct",
        "position_management.new_stop_price",
        "position_management.new_targets",
    }
    assert "HOLD_CARRIES_CHANGES" in _codes(issues)


def test_partial_close_requires_a_fraction_in_the_open_unit_interval() -> None:
    assert (
        validate_position_management(
            _payload(action="PARTIAL_CLOSE", close_fraction=0.5),
            _position(),
            mark_price=Decimal("70010"),
        )
        == ()
    )
    missing = validate_position_management(
        _payload(action="PARTIAL_CLOSE"), _position(), mark_price=Decimal("70010")
    )
    assert "CLOSE_FRACTION_REQUIRED" in _codes(missing)
    for fraction in (0.0, 1.0, -0.1, 1.5):
        issues = validate_position_management(
            _payload(action="PARTIAL_CLOSE", close_fraction=fraction),
            _position(),
            mark_price=Decimal("70010"),
        )
        assert "CLOSE_FRACTION_RANGE" in _codes(issues)


def test_partial_close_must_not_carry_an_add_percentage() -> None:
    issues = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=0.5, add_risk_pct=0.4),
        _position(),
        mark_price=Decimal("70010"),
    )
    assert "ACTION_MISMATCH" in _codes(issues)


def test_full_close_must_not_carry_a_fraction_or_an_add() -> None:
    fraction = validate_position_management(
        _payload(action="FULL_CLOSE", close_fraction=0.5),
        _position(),
        mark_price=Decimal("70010"),
    )
    add = validate_position_management(
        _payload(action="FULL_CLOSE", add_risk_pct=0.4),
        _position(),
        mark_price=Decimal("70010"),
    )
    assert "ACTION_MISMATCH" in _codes(fraction)
    assert "ACTION_MISMATCH" in _codes(add)


def test_add_requires_a_positive_risk_percentage_within_the_ceiling() -> None:
    missing = validate_position_management(
        _payload(action="ADD"), _position(), mark_price=Decimal("70010")
    )
    assert "ADD_RISK_REQUIRED" in _codes(missing)
    for value in (0.0, -1.0, 100.5):
        issues = validate_position_management(
            _payload(action="ADD", add_risk_pct=value),
            _position(),
            mark_price=Decimal("70010"),
        )
        assert "ADD_RISK_RANGE" in _codes(issues)


def test_add_rejects_a_close_fraction_or_a_stop_move() -> None:
    issues = validate_position_management(
        _payload(action="ADD", add_risk_pct=0.4, close_fraction=0.5, new_stop_price="69500"),
        _position(),
        mark_price=Decimal("70010"),
    )
    assert "ACTION_MISMATCH" in _codes(issues)
    assert "ADD_STOP_UNSUPPORTED" in _codes(issues)


def test_a_stop_on_the_wrong_side_or_unparseable_is_rejected() -> None:
    wrong_side = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=0.5, new_stop_price="72000"),
        _position(),
        mark_price=Decimal("70010"),
    )
    unparseable = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=0.5, new_stop_price="not-a-price"),
        _position(),
        mark_price=Decimal("70010"),
    )
    non_positive = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=0.5, new_stop_price="0"),
        _position(),
        mark_price=Decimal("70010"),
    )

    assert "STOP_SIDE" in _codes(wrong_side)
    assert "STOP_INVALID" in _codes(unparseable)
    assert "STOP_INVALID" in _codes(non_positive)


def test_a_valid_short_stop_below_the_mark_is_rejected_for_a_long() -> None:
    short = replace(
        _position(side=PositionSide.SHORT),
        stop_price=Decimal("71000"),
        mark_price=Decimal("70000"),
    )

    issues = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=0.5, new_stop_price="69000"),
        short,
        mark_price=Decimal("70000"),
    )

    assert "STOP_SIDE" in _codes(issues)


def test_targets_must_be_on_the_profit_side_and_ordered() -> None:
    wrong_side = validate_position_management(
        _payload(new_targets=["69000"]), _position(), mark_price=Decimal("70010")
    )
    unordered = validate_position_management(
        _payload(new_targets=["73000", "72000"]), _position(), mark_price=Decimal("70010")
    )
    unparseable = validate_position_management(
        _payload(new_targets=["nope"]), _position(), mark_price=Decimal("70010")
    )
    not_a_list = validate_position_management(
        _payload(new_targets="73000"), _position(), mark_price=Decimal("70010")
    )

    assert "TARGET_SIDE" in _codes(wrong_side)
    assert "TARGET_ORDER" in _codes(unordered)
    assert "TARGET_INVALID" in _codes(unparseable)
    assert "TARGET_INVALID" in _codes(not_a_list)


def test_ordered_targets_are_accepted() -> None:
    assert (
        validate_position_management(
            _payload(action="PARTIAL_CLOSE", close_fraction=0.5, new_targets=["73000", "74000"]),
            _position(),
            mark_price=Decimal("70010"),
        )
        == ()
    )


def test_an_unknown_action_is_rejected_before_any_field_check() -> None:
    issues = validate_position_management(
        _payload(action="SCALE_IN"), _position(), mark_price=Decimal("70010")
    )

    assert len(issues) == 1
    assert issues[0].code == "ACTION_INVALID"


def test_plan_partial_close_returns_a_step_multiple() -> None:
    plan = plan_partial_close(
        size=Decimal("0.08"),
        fraction=Decimal("0.5"),
        mark_price=Decimal("70000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
    )

    assert plan.quantity == Decimal("0.04")
    assert plan.escalated is False
    assert plan.reason is None


def test_plan_partial_close_rejects_a_slice_that_rounds_to_zero() -> None:
    with pytest.raises(ManagementSizingError) as exc:
        plan_partial_close(
            size=Decimal("0.001"),
            fraction=Decimal("0.5"),
            mark_price=Decimal("70000"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
    assert "rounds to zero" in exc.value.message


def test_plan_partial_close_escalates_when_the_remainder_is_below_the_minimum() -> None:
    plan = plan_partial_close(
        size=Decimal("0.08"),
        fraction=Decimal("0.5"),
        mark_price=Decimal("70000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("10000"),
    )

    assert plan.quantity == Decimal("0.08")
    assert plan.escalated is True
    assert plan.reason is not None and "remainder" in plan.reason


def test_plan_partial_close_rejects_unusable_inputs() -> None:
    for size, mark, fraction in (
        (Decimal("0"), Decimal("70000"), Decimal("0.5")),
        (Decimal("0.08"), Decimal("0"), Decimal("0.5")),
        (Decimal("0.08"), Decimal("70000"), Decimal("0")),
        (Decimal("0.08"), Decimal("70000"), Decimal("1")),
    ):
        with pytest.raises(ManagementSizingError):
            plan_partial_close(
                size=size,
                fraction=fraction,
                mark_price=mark,
                step_size=Decimal("0.001"),
                min_notional=Decimal("5"),
            )


def test_round_down_to_step_handles_a_missing_step() -> None:
    assert round_down_to_step(Decimal("1.2345"), Decimal("0")) == Decimal("1.2345")
    assert round_down_to_step(Decimal("1.2345"), Decimal("0.01")) == Decimal("1.23")
