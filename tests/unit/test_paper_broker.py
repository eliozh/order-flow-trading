"""Deterministic paper fills: slippage, fee, ids, and balance debit."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.paper_account import PaperAccountService, compute_equity
from oft.application.paper_broker import BrokerError, OrderIntent, PaperBroker
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.ports.store import PositionRecord

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


def test_the_broker_rejects_any_non_paper_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))
    intent = replace(_intent(), environment="LIVE")

    with pytest.raises(BrokerError) as excinfo:
        broker.submit(
            intent,
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
            now="2026-11-06T12:00:00.000Z",
        )

    assert "PAPER" in excinfo.value.message
    assert store.list_orders() == ()
    assert store.get_paper_account("ACC-PAPER") is None


def test_compute_equity_treats_a_corrupt_position_as_stale() -> None:
    equity, view = compute_equity(
        Decimal("10000.00"),
        [{"size": "not-a-number", "mark_price": "70000.00"}],
    )

    assert equity == Decimal("10000.00")
    assert view.stale_inputs == ("mark",)


def test_the_open_position_read_is_not_capped_at_one_hundred(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW))
    service.ensure()
    with sqlite3.connect(store.path) as connection:
        for index in range(101):
            connection.execute(
                "INSERT INTO positions (position_id, account_id, environment, symbol, "
                "state, opened_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"POS-{index}",
                    "ACC-PAPER",
                    "PAPER",
                    "BTCUSDT",
                    "OPEN",
                    f"2026-11-06T12:00:{index % 60:02d}.000Z",
                    json.dumps({"size": "1", "mark_price": "1.00"}),
                ),
            )

    summary = service.summary()

    assert summary["open_positions"] == 101
    assert summary["equity"] == "10101.00000000"


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


# -- paper account read models (D3) ---------------------------------------


def test_account_seeds_ten_thousand_once_and_never_overwrites(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW))

    first = service.ensure()
    second = service.ensure()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))
    broker.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )
    after_fill = service.ensure()

    assert first.initial_balance == "10000.00"
    assert first.balance == "10000.00"
    assert second.account_id == first.account_id
    assert after_fill.balance == "9299.50993000"


def test_summary_equity_adds_the_position_value_at_the_recorded_mark(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))
    broker.submit(
        _intent(),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    summary = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).summary()

    assert summary["initial_balance"] == "10000.00"
    assert summary["cash_balance"] == "9299.50993000"
    assert summary["balance"] == summary["cash_balance"]
    assert summary["open_positions"] == 1
    assert summary["unrealized_at_last_mark"] == "700.00000000"
    assert summary["equity"] == "9999.50993000"
    assert summary["currency"] == "USDT"
    assert summary["stale_inputs"] == []
    assert summary["positions"][0]["mark_price"] == "70000.00"


def test_summary_without_a_mark_reports_stale_and_never_fabricates_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW))
    service.ensure()
    unmarked = PositionRecord(
        position_id="POS-NO-MARK",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        state="OPEN",
        opened_at="2026-11-06T12:00:00.000Z",
        payload_json='{"size": "0.01", "mark_price": null}',
    )
    monkeypatch.setattr(store, "list_positions", lambda **_kwargs: (unmarked,))

    summary = service.summary()

    assert summary["cash_balance"] == "10000.00"
    assert summary["equity"] == "10000.00"
    assert summary["unrealized_at_last_mark"] == "0"
    assert summary["stale_inputs"] == ["mark"]


def test_compute_equity_ignores_a_missing_size_as_stale() -> None:
    equity, view = compute_equity(Decimal("100.00"), [{"size": None, "mark_price": "70000"}])

    assert equity == Decimal("100.00")
    assert view.stale_inputs == ("mark",)
    assert view.available is False


def test_compute_equity_treats_non_finite_size_or_mark_as_stale() -> None:
    for position in (
        {"size": "NaN", "mark_price": "70000"},
        {"size": "0.01", "mark_price": "Infinity"},
        {"size": "-Infinity", "mark_price": "70000"},
    ):
        equity, view = compute_equity(Decimal("100.00"), [position])

        assert equity == Decimal("100.00"), position
        assert view.stale_inputs == ("mark",), position
        assert view.available is False, position


def test_paper_bps_settings_must_be_bounded_finite_decimals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.config import ConfigError, load_settings

    for name, value in (
        ("OFT_PAPER_SLIPPAGE_BPS", "nan"),
        ("OFT_PAPER_SLIPPAGE_BPS", "inf"),
        ("OFT_PAPER_SLIPPAGE_BPS", "-1"),
        ("OFT_PAPER_TAKER_FEE_BPS", "10001"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        assert excinfo.value.code == "CONFIG_INVALID"
        monkeypatch.delenv(name)


def test_compute_equity_names_mark_once_for_several_unmarked_positions() -> None:
    equity, view = compute_equity(
        Decimal("100.00"),
        [
            {"size": "0.01", "mark_price": None},
            {"size": "0.02", "mark_price": None},
        ],
    )

    assert equity == Decimal("100.00")
    assert view.stale_inputs == ("mark",)


def test_compute_equity_with_marks_is_available() -> None:
    equity, view = compute_equity(Decimal("100.00"), [{"size": "0.01", "mark_price": "70000.00"}])

    assert equity == Decimal("800.00")
    assert view.marked_value == Decimal("700.00000000")
    assert view.available is True


def test_account_delegates_to_the_seed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW))

    assert service.account().balance == "10000.00"


def test_no_account_has_fake_fills_before_any_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW))

    assert service.orders() == ()
    assert service.fills() == ()
    assert service.summary()["equity"] == "10000.00"


def test_market_fill_is_whole_not_partial(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    broker = PaperBroker(settings=settings, store=store, clock=FixedClock(NOW))

    result = broker.submit(
        _intent(quantity="0.01"),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
        now="2026-11-06T12:00:00.000Z",
    )

    assert result.order.status.value == "FILLED"
    assert result.order.filled_quantity == result.order.quantity
    assert result.fill.quantity == result.order.quantity
    assert len(store.list_fills()) == 1
