"""Exact branch coverage for the deterministic pnl/management/positions code.

These are the defensive and escalation branches the 100% branch gate requires.
Every case asserts the typed outcome; none is a pragma or a skip.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from oft.domain.management import (
    ManagementSizingError,
    PartialClosePlan,
    plan_partial_close,
    validate_position_management,
)
from oft.domain.oms import Fill, OrderSide, Position, PositionSide
from oft.domain.pnl import (
    ZERO,
    first_touch,
    funding_amount_usd,
    json_decimal,
    mae_mfe_from_bars,
    mae_mfe_from_sized_bars,
    r_multiple,
    return_percent,
)
from oft.domain.positions import (
    FULL_CLOSE,
    PARTIAL_CLOSE,
    CloseOutcome,
    CloseResult,
    ManagementEvent,
    PositionError,
    apply_add,
    close_full,
    evaluate_protective_exit,
    partial_close,
    payload_has_partial_close,
    position_from_contract,
    trade_id_for,
    trade_log_from_close,
)

NOW = "2026-11-14T12:00:00.000Z"
ENTRY = "2026-11-14T11:00:00.000Z"


# -- pnl defensive branches -------------------------------------------------


def test_funding_rejects_an_implausible_position_size() -> None:
    with pytest.raises(ValueError, match="position size"):
        funding_amount_usd("LONG", Decimal("0.0001"), Decimal("70000"), Decimal("1e100"))


def test_mae_mfe_skip_a_bar_with_an_unparseable_low() -> None:
    mae, mfe = mae_mfe_from_bars(
        "LONG",
        Decimal("70000"),
        Decimal("1"),
        ({"low": object(), "high": "1"}, {"low": "69000", "high": "71000"}),
    )
    assert mae == Decimal("-1000.00000000")
    assert mfe == Decimal("1000.00000000")


def test_mae_mfe_reject_negative_sizes() -> None:
    with pytest.raises(ValueError, match="size must be non-negative"):
        mae_mfe_from_bars("LONG", Decimal("70000"), Decimal("-1"), ())
    with pytest.raises(ValueError, match="size must be non-negative"):
        mae_mfe_from_sized_bars(
            "LONG", ({"low": "69000", "high": "71000"},), ((Decimal("70000"), Decimal("-1")),)
        )


def test_an_implausible_r_or_return_is_unmeasured_not_a_crash() -> None:
    assert r_multiple(Decimal("1e100"), Decimal("1")) is None
    assert return_percent(Decimal("1e100"), Decimal("1")) == 0.0


def test_an_absent_json_decimal_is_none() -> None:
    assert json_decimal(None) is None


def test_first_touch_is_still_deterministic_without_a_stop_or_target() -> None:
    with pytest.raises(ValueError):
        first_touch("LONG", (), stop_price=None, target_price=None)


# -- management defensive and escalation branches ---------------------------


@pytest.mark.parametrize(
    ("size", "quantity"),
    [
        (Decimal("0"), Decimal("1")),
        (Decimal("NaN"), Decimal("1")),
        (Decimal("1"), Decimal("0")),
        (Decimal("1"), Decimal("NaN")),
    ],
)
def test_partial_close_plan_requires_positive_finite_sizes(
    size: Decimal, quantity: Decimal
) -> None:
    with pytest.raises(ValueError, match="positive"):
        PartialClosePlan(size=size, quantity=quantity, escalated=False, reason=None)


def _position(*, risk: str = "1000") -> Position:
    return Position(
        position_id="POS-1",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="OPEN",
        size=Decimal("1"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("70000"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("35000"),
        risk_amount_usd=Decimal(risk),
        risk_pct=1.0,
        fees_usd=Decimal("10"),
        opened_at=ENTRY,
        updated_at=ENTRY,
        source_trigger_id="TRG-1",
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "HOLD",
        "close_fraction": None,
        "add_risk_pct": None,
        "new_stop_price": None,
        "new_targets": [],
    }
    payload.update(overrides)
    return payload


def test_a_boolean_close_fraction_is_not_a_number() -> None:
    issues = validate_position_management(
        _payload(action="PARTIAL_CLOSE", close_fraction=True),
        _position(),
        mark_price=Decimal("70000"),
    )
    assert {issue.code for issue in issues} == {"CLOSE_FRACTION_RANGE"}


def test_a_boolean_add_risk_is_not_a_number() -> None:
    issues = validate_position_management(
        _payload(action="ADD", add_risk_pct=True),
        _position(),
        mark_price=Decimal("70000"),
    )
    assert {issue.code for issue in issues} == {"ADD_RISK_RANGE"}


def test_an_absent_targets_field_is_treated_as_no_targets() -> None:
    payload = _payload(action="PARTIAL_CLOSE", close_fraction=0.5)
    payload.pop("new_targets")

    assert validate_position_management(payload, _position(), mark_price=Decimal("70000")) == ()


def test_a_rounding_edge_escalates_a_partial_close_to_the_whole_position() -> None:
    plan = plan_partial_close(
        size=Decimal("1"),
        fraction=Decimal("0.9999999999999999999999999999"),
        mark_price=Decimal("70000"),
        step_size=Decimal("0.5"),
        min_notional=Decimal("0"),
    )

    assert plan.escalated is True
    assert plan.quantity == Decimal("1")
    assert plan.reason is not None and "whole position" in plan.reason


def test_an_unusable_step_is_a_typed_sizing_error() -> None:
    with pytest.raises(ManagementSizingError, match="implausible quantity"):
        plan_partial_close(
            size=Decimal("1"),
            fraction=Decimal("0.5"),
            mark_price=Decimal("70000"),
            step_size=Decimal("NaN"),
            min_notional=Decimal("0"),
        )


def test_an_implausible_remainder_notional_is_a_typed_sizing_error() -> None:
    with pytest.raises(ManagementSizingError, match="implausible notional"):
        plan_partial_close(
            size=Decimal("1"),
            fraction=Decimal("0.5"),
            mark_price=Decimal("1e100"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("0"),
        )


# -- positions defensive branches -------------------------------------------


def _fill(
    *,
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.SELL,
    price: str = "69900",
    quantity: str = "1",
    fee: str = "1",
) -> Fill:
    return Fill(
        fill_id="FIL-1",
        order_id="ORD-1",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol=symbol,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        quote_quantity=Decimal(price) * Decimal(quantity),
        fee=Decimal(fee),
        fee_asset="USDT",
        maker=False,
        filled_at=NOW,
        received_at=NOW,
    )


def _closed_position() -> Position:
    return replace(
        _position(),
        state="CLOSED",
        closed_at=NOW,
        exit_price=Decimal("69900"),
        realized_pnl_usd=Decimal("-100"),
    )


def test_close_outcome_notes_name_every_outcome() -> None:
    assert CloseOutcome.TARGET_TOUCH.note == "target touched"
    assert CloseOutcome.MANUAL_CLOSE.note == "manual close"
    assert CloseOutcome.STOP_TOUCH.note == "protective stop touched"


def test_close_result_requires_a_full_close_event() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="MGD-1", accumulated_gross=ZERO)
    event = ManagementEvent(
        decision_id="MGD-1", action=PARTIAL_CLOSE, at=NOW, order_id="ORD-1", note="partial"
    )

    with pytest.raises(ValueError, match="FULL_CLOSE event"):
        replace(close, event=event)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size", "1e100", "size is outside"),
        ("avg_entry_price", "1e100", "average entry price"),
        ("notional_usd", "1e100", "notional is outside"),
    ],
)
def test_position_from_contract_bounds_every_load_bearing_magnitude(
    field: str, value: str, message: str
) -> None:
    payload = _position().to_contract()
    payload[field] = value

    with pytest.raises(PositionError) as excinfo:
        position_from_contract(payload)

    assert message in excinfo.value.message


def test_only_an_open_position_can_be_evaluated_or_modified() -> None:
    closed = _closed_position()

    with pytest.raises(PositionError, match="only an OPEN position can be evaluated"):
        evaluate_protective_exit(closed, Decimal("69900"), at=NOW)
    with pytest.raises(PositionError, match="only an OPEN position can be reduced"):
        partial_close(closed, _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)
    with pytest.raises(PositionError, match="only an OPEN position can be added to"):
        apply_add(
            closed,
            _fill(side=OrderSide.BUY, price="71000"),
            at=NOW,
            decision_id="M",
            accumulated_gross=ZERO,
        )
    with pytest.raises(PositionError, match="only an OPEN position can be closed"):
        close_full(closed, _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)


def test_a_non_positive_mark_is_not_evaluated() -> None:
    with pytest.raises(PositionError, match="mark price must be positive"):
        evaluate_protective_exit(_position(), Decimal("0"), at=NOW)


def test_a_corrupt_history_container_fails_closed() -> None:
    with pytest.raises(PositionError, match="management history is corrupt"):
        payload_has_partial_close({"management_history": "corrupt"})


@pytest.mark.parametrize(
    "mutate_call",
    [
        pytest.param(
            lambda: partial_close(
                _position(),
                _fill(symbol="ETHUSDT"),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="partial-symbol",
        ),
        pytest.param(
            lambda: apply_add(
                _position(),
                _fill(side=OrderSide.BUY, symbol="ETHUSDT", price="71000"),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="add-symbol",
        ),
        pytest.param(
            lambda: close_full(
                _position(),
                _fill(symbol="ETHUSDT"),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="close-symbol",
        ),
    ],
)
def test_a_fill_for_another_symbol_is_rejected(mutate_call: object) -> None:
    with pytest.raises(PositionError, match="symbol does not match"):
        mutate_call()  # type: ignore[operator]


@pytest.mark.parametrize(
    "mutate_call",
    [
        pytest.param(
            lambda: partial_close(
                replace(_position(), notional_usd=Decimal("1e100")),
                _fill(quantity="0.5"),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="partial",
        ),
        pytest.param(
            lambda: apply_add(
                replace(_position(), notional_usd=Decimal("1e100")),
                _fill(side=OrderSide.BUY, price="71000", quantity="1"),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="add",
        ),
        pytest.param(
            lambda: close_full(
                replace(_position(), fees_usd=Decimal("1e100")),
                _fill(),
                at=NOW,
                decision_id="M",
                accumulated_gross=ZERO,
            ),
            id="close",
        ),
    ],
)
def test_implausible_money_math_is_a_typed_unavailable_error(mutate_call: object) -> None:
    with pytest.raises(PositionError) as excinfo:
        mutate_call()  # type: ignore[operator]

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "implausible" in excinfo.value.message


def _valid_context(close: CloseResult, **overrides: object) -> object:
    from oft.domain.positions import TradeLogContext

    values: dict[str, object] = {
        "signal": {},
        "provenance": {},
        "decision_quality": {},
        "mae_usd": Decimal("-1"),
        "mfe_usd": Decimal("1"),
        "risk_amount_usd": close.position.risk_amount_usd,
        "entry_notional_usd": Decimal("70000"),
        "data_quality_status": "COMPLETE",
        "data_quality_reasons": (),
        "stopped": True,
        "traded_quantity": Decimal("1"),
    }
    values.update(overrides)
    return TradeLogContext(**values)  # type: ignore[arg-type]


def _render(close: CloseResult, context: object, **kwargs: object) -> dict[str, object]:
    return trade_log_from_close(
        close, trade_id=trade_id_for(close.position.position_id), context=context, **kwargs
    )


def test_an_unmeasured_entry_notional_without_a_partial_reason_is_rejected() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="requires a named PARTIAL reason"):
        _render(close, _valid_context(close, entry_notional_usd=None))


def test_a_non_positive_entry_notional_is_rejected() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="must be positive"):
        _render(close, _valid_context(close, entry_notional_usd=Decimal("0")))


def test_the_entry_notional_is_reconciled_with_the_entry_fill_when_known() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="does not match the recorded entry fill"):
        _render(
            close,
            _valid_context(close),
            expected_entry_notional_usd=Decimal("70001"),
        )


def test_a_matching_expected_entry_notional_and_quantity_render_cleanly() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    trade = _render(
        close,
        _valid_context(close),
        expected_entry_notional_usd=Decimal("70000"),
        expected_traded_quantity=Decimal("1"),
    )

    assert trade["size"] == "1"
    assert trade["data_quality"]["status"] == "COMPLETE"


def test_the_entry_notional_cannot_understate_the_position_notional() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="below the position's current notional"):
        _render(close, _valid_context(close, entry_notional_usd=Decimal("1")))


def test_a_positive_mae_or_negative_mfe_is_rejected() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="MAE must be"):
        _render(close, _valid_context(close, mae_usd=Decimal("1")))


def test_a_recorded_partial_with_an_expected_quantity_must_trade_more_than_the_residual() -> None:
    first = partial_close(
        _position(), _fill(quantity="0.5"), at=NOW, decision_id="M1", accumulated_gross=ZERO
    )
    remaining = position_from_contract(first.payload)
    close = close_full(
        remaining,
        _fill(quantity="0.5", price="69900"),
        at=NOW,
        decision_id="M2",
        accumulated_gross=first.accumulated_gross_pnl_usd,
    )
    context = _valid_context(
        close,
        had_partial_close=True,
        traded_quantity=close.position.size,
    )

    with pytest.raises(PositionError, match="must trade more than the open size"):
        _render(close, context, expected_traded_quantity=close.position.size)


def test_an_unknown_data_quality_status_is_rejected() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="unknown data-quality status"):
        _render(close, _valid_context(close, data_quality_status="UNKNOWN"))


def test_a_partial_trade_log_must_name_a_reason() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)

    with pytest.raises(PositionError, match="must name at least one reason"):
        _render(close, _valid_context(close, data_quality_status="PARTIAL"))


def test_an_unmeasured_r_adds_the_r_multiple_reason() -> None:
    close = close_full(
        replace(_position(), risk_amount_usd=ZERO),
        _fill(),
        at=NOW,
        decision_id="M",
        accumulated_gross=ZERO,
    )
    trade = _render(close, _valid_context(close, risk_amount_usd=ZERO))

    assert trade["r_multiple"] is None
    assert any("R multiple" in reason for reason in trade["data_quality"]["reasons"])


def test_an_unmeasured_entry_notional_adds_the_return_reason() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id="M", accumulated_gross=ZERO)
    trade = _render(
        close,
        _valid_context(
            close,
            entry_notional_usd=None,
            data_quality_status="PARTIAL",
            data_quality_reasons=("a measured gap",),
        ),
    )

    assert any("entry notional" in reason for reason in trade["data_quality"]["reasons"])


def test_a_partial_close_that_empties_the_position_is_rejected_twice() -> None:
    with pytest.raises(PositionError, match="must leave a positive size"):
        partial_close(
            _position(), _fill(quantity="1"), at=NOW, decision_id="M", accumulated_gross=ZERO
        )


def test_a_full_close_requires_the_whole_open_size() -> None:
    with pytest.raises(PositionError, match="only a full close is supported"):
        close_full(
            _position(),
            _fill(quantity="0.5"),
            at=NOW,
            decision_id="M",
            accumulated_gross=ZERO,
        )


def test_a_full_close_event_keeps_its_outcome_note() -> None:
    close = close_full(
        _position(),
        _fill(),
        at=NOW,
        decision_id="M",
        accumulated_gross=ZERO,
        close_reason="TARGET_TOUCH",
    )

    assert close.event.action == FULL_CLOSE
    assert close.event.note == "target touched"
    assert close.outcome is CloseOutcome.TARGET_TOUCH
