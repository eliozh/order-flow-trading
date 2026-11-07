"""Deterministic paper fills: slippage, fee, ids, and balance debit."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _intent(*, quantity: str = "0.01", idempotency_key: str = "idem-1") -> OrderIntent:
    return OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=None,
        stop_price=Decimal("69000.00"),
        take_profits=(Decimal("72000.00"),),
        leverage=2.0,
        decision_id="DEC-1",
        risk_verdict_id="RISK-1",
        approval_id="APR-1",
        idempotency_key=idempotency_key,
        position_side=PositionSide.LONG,
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
    )


def test_market_fill_applies_two_bps_slippage_and_five_bps_fee(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))

    result = broker.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    assert result.order.status.value == "FILLED"
    assert result.fill.price == Decimal("70014.00000000")
    assert result.fill.quantity == Decimal("0.01")
    assert result.fill.quote_quantity == Decimal("700.14000000")
    assert result.fill.fee == Decimal("0.35007000")
    assert result.fill.maker is False
    assert result.position.state == "OPEN"
    assert result.position.side == PositionSide.LONG
    assert result.position.entry_price == result.fill.price
    assert result.account.balance == "9299.50993000"


def test_short_fill_applies_slippage_downward(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("71000.00"),
        take_profits=(),
        leverage=2.0,
        decision_id="DEC-1",
        risk_verdict_id="RISK-1",
        approval_id="APR-1",
        idempotency_key="idem-short",
        position_side=PositionSide.SHORT,
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
    )

    result = broker.submit(
        intent,
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    assert result.fill.price == Decimal("69986.00000000")
    assert result.position.side == PositionSide.SHORT


def test_same_inputs_and_clock_produce_identical_ids_and_values(tmp_path: Path) -> None:
    first_settings = _settings(tmp_path / "first")
    second_settings = _settings(tmp_path / "second")
    first_store = SqliteStore(first_settings)
    second_store = SqliteStore(second_settings)
    first_store.initialize()
    second_store.initialize()
    first = PaperBroker(settings=first_settings, store=first_store, clock=FixedClock(NOW))
    second = PaperBroker(settings=second_settings, store=second_store, clock=FixedClock(NOW))

    left = first.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )
    right = second.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    assert left.order.order_id == right.order.order_id
    assert left.order.client_order_id == right.order.client_order_id
    assert left.fill.fill_id == right.fill.fill_id
    assert left.fill.price == right.fill.price
    assert left.position.position_id == right.position.position_id
    assert left.account.balance == right.account.balance


def test_records_are_visible_through_the_store(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))

    result = broker.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    assert store.get_order(result.order.order_id) is not None
    assert len(store.list_fills()) == 1
    assert len(store.list_positions(state="OPEN")) == 1
    account = store.get_paper_account("ACC-PAPER")
    assert account is not None
    assert account.balance == "9299.50993000"
