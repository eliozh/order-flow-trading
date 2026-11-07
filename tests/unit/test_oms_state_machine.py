"""OMS state machine and deterministic identifier tests (ADR-0006)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.oms import (
    Fill,
    Order,
    OrderSide,
    OrderStateError,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    client_order_id,
    intent_hash,
    order_from_intent,
    position_from_fill,
)


def _order(status: OrderStatus = OrderStatus.CREATED) -> Order:
    return order_from_intent(
        order_id="ORD-1",
        client_order_id_value="oft-paper-btcusdt-entry-abc",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("69000.00"),
        risk_verdict_id="RISK-1",
        approval_id="APR-1",
        intent_hash_value="deadbeef",
        idempotency_key="idem-1",
        created_at="2026-11-06T12:00:00.000Z",
        status=status,
    )


def test_client_order_id_is_deterministic_and_bounded() -> None:
    first = client_order_id("PAPER", "BTCUSDT", "entry", "abc123")
    second = client_order_id("PAPER", "BTCUSDT", "entry", "abc123")
    other = client_order_id("PAPER", "BTCUSDT", "entry", "abc124")

    assert first == second
    assert first.startswith("oft-paper-BTCUSDT-entry-")
    assert len(first) <= 64
    assert other != first


def test_intent_hash_is_canonical() -> None:
    left = {"symbol": "BTCUSDT", "quantity": "0.01", "side": "BUY"}
    right = {"side": "BUY", "quantity": "0.01", "symbol": "BTCUSDT"}

    assert intent_hash(left) == intent_hash(right)
    assert intent_hash({**left, "quantity": "0.02"}) != intent_hash(left)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (OrderStatus.CREATED, OrderStatus.SUBMITTED),
        (OrderStatus.CREATED, OrderStatus.REJECTED),
        (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.CANCELED),
        (OrderStatus.SUBMITTED, OrderStatus.EXPIRED),
        (OrderStatus.SUBMITTED, OrderStatus.UNKNOWN),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN),
    ],
)
def test_allowed_transitions(start: OrderStatus, target: OrderStatus) -> None:
    order = _order(start)

    transitioned = order.transition(target, at="2026-11-06T12:00:01.000Z")

    assert transitioned.status == target
    assert transitioned.updated_at == "2026-11-06T12:00:01.000Z"


def test_terminal_states_reject_further_transitions() -> None:
    for terminal in (
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    ):
        with pytest.raises(OrderStateError):
            _order(terminal).transition(OrderStatus.SUBMITTED, at="t")


def test_unknown_transition_is_rejected() -> None:
    with pytest.raises(OrderStateError):
        _order(OrderStatus.CREATED).transition(OrderStatus.FILLED, at="t")


def test_fill_requires_positive_quantity() -> None:
    with pytest.raises(ValueError):
        Fill(
            fill_id="FIL-1",
            order_id="ORD-1",
            environment="PAPER",
            account_id="ACC-PAPER",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("70000.00"),
            quantity=Decimal("0"),
            quote_quantity=Decimal("0"),
            fee=Decimal("0"),
            fee_asset="USDT",
            maker=False,
            filled_at="t",
            received_at="t",
            source="PAPER",
        )


def test_fill_requires_a_positive_price() -> None:
    with pytest.raises(ValueError):
        Fill(
            fill_id="FIL-1",
            order_id="ORD-1",
            environment="PAPER",
            account_id="ACC-PAPER",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("0"),
            quantity=Decimal("1"),
            quote_quantity=Decimal("0"),
            fee=Decimal("0"),
            fee_asset="USDT",
            maker=False,
            filled_at="t",
            received_at="t",
            source="PAPER",
        )


def test_unfilled_order_has_no_quote_quantity() -> None:
    assert _order().quote_quantity is None


def test_position_from_fill_opens_long_and_short() -> None:
    fill = Fill(
        fill_id="FIL-1",
        order_id="ORD-1",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("70000.00"),
        quantity=Decimal("0.01"),
        quote_quantity=Decimal("700.00"),
        fee=Decimal("0.35"),
        fee_asset="USDT",
        maker=False,
        filled_at="t",
        received_at="t",
        source="PAPER",
    )
    long_position = position_from_fill(
        fill,
        position_id="POS-1",
        side=PositionSide.LONG,
        stop_price=Decimal("69000.00"),
        target_prices=(Decimal("72000.00"),),
        leverage=2,
        mark_price=Decimal("70000.00"),
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
        opened_at="t",
    )
    assert long_position.state == "OPEN"
    assert long_position.side == PositionSide.LONG
    assert long_position.size == Decimal("0.01")
    assert long_position.fees_usd == Decimal("0.35")

    short_position = position_from_fill(
        fill,
        position_id="POS-2",
        side=PositionSide.SHORT,
        stop_price=Decimal("71000.00"),
        target_prices=(),
        leverage=2,
        mark_price=Decimal("70000.00"),
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
        opened_at="t",
    )
    assert short_position.side == PositionSide.SHORT


def test_position_requires_open_state() -> None:
    with pytest.raises(ValueError):
        Position(
            position_id="POS-1",
            account_id="ACC-PAPER",
            environment="PAPER",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            state="CLOSED",
            size=Decimal("0.01"),
            entry_price=Decimal("1"),
            avg_entry_price=Decimal("1"),
            mark_price=Decimal("1"),
            notional_usd=Decimal("0.01"),
            stop_price=None,
            target_prices=(),
            leverage=2,
            margin_usd=Decimal("0.005"),
            risk_amount_usd=Decimal("0"),
            risk_pct=0.0,
            fees_usd=Decimal("0"),
            opened_at="t",
            updated_at="t",
        )


def test_position_requires_a_positive_size() -> None:
    with pytest.raises(ValueError):
        Position(
            position_id="POS-1",
            account_id="ACC-PAPER",
            environment="PAPER",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            state="OPEN",
            size=Decimal("0"),
            entry_price=Decimal("1"),
            avg_entry_price=Decimal("1"),
            mark_price=Decimal("1"),
            notional_usd=Decimal("0"),
            stop_price=None,
            target_prices=(),
            leverage=2,
            margin_usd=Decimal("0"),
            risk_amount_usd=Decimal("0"),
            risk_pct=0.0,
            fees_usd=Decimal("0"),
            opened_at="t",
            updated_at="t",
        )
