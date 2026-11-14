"""The one order-intent representation and its management classification."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.application.execution_gateway import classify_intent
from oft.application.paper_broker import OrderIntent
from oft.domain.oms import OrderSide, OrderType, PositionSide


def _intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": OrderSide.SELL,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.04"),
        "price": None,
        "stop_price": Decimal("69000"),
        "take_profits": (),
        "leverage": 2.0,
        "decision_id": "MGD-1",
        "risk_verdict_id": "RISK-1",
        "approval_id": "APR-1",
        "idempotency_key": "mgmt-1",
        "position_side": PositionSide.LONG,
        "risk_amount_usd": Decimal("0"),
        "risk_pct": 0.0,
        "position_id": "POS-1",
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_partial_and_full_close_are_reduce_only_from_the_one_representation() -> None:
    for purpose in ("PARTIAL_CLOSE", "partial_close", "FULL_CLOSE"):
        intent = _intent(purpose=purpose)
        assert intent.purpose == purpose.upper()
        assert intent.reduce_only is True
        assert classify_intent(intent).risk_reducing is True


def test_an_add_is_not_reduce_only_and_is_risk_increasing() -> None:
    intent = _intent(purpose="ADD", side=OrderSide.BUY)

    assert intent.purpose == "ADD"
    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_an_exit_stays_reduce_only() -> None:
    intent = _intent(purpose="EXIT")

    assert intent.reduce_only is True
    assert classify_intent(intent).risk_reducing is True


def test_an_entry_is_never_treated_as_a_reduce_only_management_order() -> None:
    intent = _intent(purpose="ENTRY", reduce_only=True)

    assert intent.purpose == "ENTRY"
    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_the_intent_hash_binds_the_normalized_purpose() -> None:
    partial = _intent(purpose="partial_close")
    full = _intent(purpose="FULL_CLOSE")

    assert partial.hash() != full.hash()
    assert partial.hash() == _intent(purpose="PARTIAL_CLOSE").hash()


def test_the_close_reason_is_a_journal_fact_and_not_part_of_the_intent_hash() -> None:
    stopped = _intent(purpose="FULL_CLOSE", close_reason="STOP_TOUCH")
    manual = _intent(purpose="FULL_CLOSE", close_reason="MANUAL_CLOSE")

    assert stopped.close_reason == "STOP_TOUCH"
    assert manual.close_reason == "MANUAL_CLOSE"
    assert stopped.hash() == manual.hash()


def test_an_unknown_purpose_is_risk_increasing() -> None:
    intent = _intent(purpose="REBALANCE")

    assert intent.reduce_only is False
    assert classify_intent(intent).risk_reducing is False


def test_classification_is_idempotent_for_the_same_intent() -> None:
    intent = _intent(purpose="PARTIAL_CLOSE")
    assert classify_intent(intent) == classify_intent(intent)


@pytest.mark.parametrize("purpose", ("PARTIAL_CLOSE", "FULL_CLOSE"))
def test_a_reduce_only_purpose_derives_the_flag_even_when_the_caller_says_no(
    purpose: str,
) -> None:
    intent = _intent(purpose=purpose, reduce_only=False)
    assert intent.reduce_only is True
