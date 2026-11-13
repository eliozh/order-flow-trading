"""Domain: partial close and add accounting keep one OPEN position."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from oft.domain.oms import Fill, OrderSide, Position, PositionSide
from oft.domain.pnl import return_percent
from oft.domain.positions import (
    ADD,
    PARTIAL_CLOSE,
    PositionError,
    TradeLogContext,
    apply_add,
    close_full,
    partial_close,
    position_from_contract,
    trade_id_for,
    trade_log_from_close,
)

NOW = "2026-11-14T12:00:00.000Z"
ENTRY = "2026-11-14T11:00:00.000Z"


def _position(
    *,
    side: PositionSide = PositionSide.LONG,
    size: str = "1",
    entry: str = "70000",
    stop: str = "69000",
    fees: str = "10",
    funding: str = "0",
) -> Position:
    return Position(
        position_id="POS-1",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=side,
        state="OPEN",
        size=Decimal(size),
        entry_price=Decimal(entry),
        avg_entry_price=Decimal(entry),
        mark_price=Decimal(entry),
        notional_usd=Decimal(entry) * Decimal(size),
        stop_price=Decimal(stop),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal(entry) * Decimal(size) / Decimal("2"),
        risk_amount_usd=Decimal("1000"),
        risk_pct=1.0,
        fees_usd=Decimal(fees),
        funding_usd=Decimal(funding),
        opened_at=ENTRY,
        updated_at=ENTRY,
        source_trigger_id="TRG-1",
    )


def _fill(
    *,
    side: OrderSide = OrderSide.SELL,
    price: str = "70500",
    quantity: str = "0.5",
    fee: str = "17.625",
) -> Fill:
    return Fill(
        fill_id="FIL-1",
        order_id="ORD-1",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol="BTCUSDT",
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


def test_partial_close_halves_size_and_keeps_the_position_open() -> None:
    result = partial_close(
        _position(),
        _fill(),
        at=NOW,
        decision_id="MGD-1",
    )

    payload = result.payload
    assert payload["state"] == "OPEN"
    assert payload["size"] == "0.5"
    assert payload["closed_at"] is None
    assert payload["exit_price"] is None
    assert result.gross_pnl_usd == Decimal("250.00000000")
    assert result.accumulated_gross_pnl_usd == Decimal("250.00000000")
    assert Decimal(payload["realized_pnl_usd"]) == Decimal("250.00000000")
    assert Decimal(payload["fees_usd"]) == Decimal("27.62500000")
    assert payload["management_history"][-1]["action"] == PARTIAL_CLOSE
    assert payload["management_history"][-1]["executed_order_id"] == "ORD-1"
    assert payload["version"] == 2


def test_a_second_partial_close_accumulates_the_gross_total() -> None:
    first = partial_close(_position(size="1"), _fill(quantity="0.4"), at=NOW, decision_id="M1")
    position = Position(
        position_id="POS-1",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="OPEN",
        size=Decimal(str(first.payload["size"])),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("70000"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("35000"),
        risk_amount_usd=Decimal("1000"),
        risk_pct=1.0,
        fees_usd=Decimal(str(first.payload["fees_usd"])),
        opened_at=ENTRY,
        updated_at=NOW,
        source_trigger_id="TRG-1",
    )
    second = partial_close(
        position,
        _fill(quantity="0.3"),
        at="2026-11-14T13:00:00.000Z",
        decision_id="M2",
        accumulated_gross=first.accumulated_gross_pnl_usd,
    )
    assert second.accumulated_gross_pnl_usd == Decimal("350.00000000")
    assert second.payload["size"] == "0.3"


def test_a_full_close_after_partials_totals_every_slice() -> None:
    first = partial_close(_position(), _fill(), at=NOW, decision_id="M1")
    position = Position(
        position_id="POS-1",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="OPEN",
        size=Decimal("0.5"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("70000"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("35000"),
        risk_amount_usd=Decimal("1000"),
        risk_pct=1.0,
        fees_usd=Decimal("27.625"),
        opened_at=ENTRY,
        updated_at=NOW,
        source_trigger_id="TRG-1",
    )
    close = close_full(
        position,
        _fill(quantity="0.5", price="69900", fee="17.475"),
        at="2026-11-14T13:00:00.000Z",
        decision_id="M2",
        accumulated_gross=first.accumulated_gross_pnl_usd,
    )
    # 250 (first slice) + (-50) (second slice) = 200 gross.
    assert close.gross_pnl_usd == Decimal("200.00000000")
    assert close.fees_usd == Decimal("45.10000000")
    assert close.net_pnl_usd == Decimal("154.90000000")
    assert close.position.state == "CLOSED"


def test_a_partial_close_that_empties_the_position_is_rejected() -> None:
    with pytest.raises(PositionError):
        partial_close(_position(), _fill(quantity="1"), at=NOW, decision_id="M1")
    # A non-positive fill is rejected by the fill itself.
    with pytest.raises(ValueError):
        partial_close(_position(), _fill(quantity="0"), at=NOW, decision_id="M1")


def test_partial_close_side_and_symbol_must_match() -> None:
    with pytest.raises(PositionError):
        partial_close(_position(), _fill(side=OrderSide.BUY), at=NOW, decision_id="M1")
    with pytest.raises(PositionError):
        partial_close(
            _position(side=PositionSide.SHORT, stop="71000"),
            _fill(side=OrderSide.SELL),
            at=NOW,
            decision_id="M1",
        )


def test_apply_add_moves_size_and_weighted_average_entry() -> None:
    result = apply_add(
        _position(),
        _fill(side=OrderSide.BUY, price="71000", quantity="1", fee="35.50"),
        at=NOW,
        decision_id="M-ADD",
    )

    position = result.position
    assert position.size == Decimal("2")
    assert position.avg_entry_price == Decimal("70500.00000000")
    assert position.state == "OPEN"
    # risk grows by |71000 - 69000| * 1 = 2000.
    assert position.risk_amount_usd == Decimal("3000.00000000")
    assert position.fees_usd == Decimal("45.50000000")
    assert position.notional_usd == Decimal("141000.00000000")
    assert result.payload["management_history"][-1]["action"] == ADD
    assert result.payload["closed_at"] is None


def test_apply_add_requires_a_stop_and_the_increasing_side() -> None:
    with pytest.raises(PositionError):
        apply_add(
            _position(stop="0"),
            _fill(side=OrderSide.BUY, price="71000", quantity="1"),
            at=NOW,
            decision_id="M-ADD",
        )
    with pytest.raises(PositionError):
        apply_add(
            _position(),
            _fill(side=OrderSide.SELL, price="71000", quantity="1"),
            at=NOW,
            decision_id="M-ADD",
        )


def test_apply_add_rejects_a_stop_on_the_wrong_side_of_the_add_price() -> None:
    with pytest.raises(PositionError):
        apply_add(
            _position(),
            _fill(side=OrderSide.BUY, price="68000", quantity="1"),
            at=NOW,
            decision_id="M-ADD",
        )


def test_apply_add_rejects_a_closed_position() -> None:
    closed = replace(
        _position(),
        state="CLOSED",
        closed_at=NOW,
        exit_price=Decimal("70000"),
        realized_pnl_usd=Decimal("0"),
    )
    with pytest.raises(PositionError):
        apply_add(
            closed,
            _fill(side=OrderSide.BUY, price="71000", quantity="1"),
            at=NOW,
            decision_id="M-ADD",
        )


def test_partial_close_reduces_notional_and_margin_proportionally() -> None:
    result = partial_close(_position(), _fill(), at=NOW, decision_id="M1")

    payload = result.payload
    assert Decimal(payload["size"]) == Decimal("0.5")
    assert Decimal(payload["notional_usd"]) == Decimal("35000")
    assert Decimal(payload["margin_usd"]) == Decimal("17500")
    # The frozen initial risk is untouched.
    assert Decimal(payload["risk_amount_usd"]) == Decimal("1000")
    # The fill price is persisted so the stored mark stays coherent with
    # ``updated_at`` and the stale-mark rule keeps working.
    assert Decimal(payload["mark_price"]) == Decimal("70500")


def test_apply_add_persists_the_fill_mark_and_accumulated_gross() -> None:
    result = apply_add(
        _position(),
        _fill(side=OrderSide.BUY, price="71000", quantity="1", fee="35.50"),
        at=NOW,
        decision_id="M-ADD",
        accumulated_gross=Decimal("250"),
    )

    assert result.position.mark_price == Decimal("71000")
    assert result.payload["mark_price"] == "71000"
    # The pure Position stays OPEN with no realized PnL, but the payload keeps
    # the gross already realized by earlier partial closes.
    assert result.position.realized_pnl_usd is None
    assert result.payload["realized_pnl_usd"] == "250.00000000"


def test_the_trade_log_size_is_the_total_closed_quantity_and_tags_partial() -> None:
    first = partial_close(_position(), _fill(), at=NOW, decision_id="M1")
    remaining = position_from_contract(first.payload)
    close = close_full(
        remaining,
        _fill(quantity="0.5", price="69900", fee="17.475"),
        at="2026-11-14T13:00:00.000Z",
        decision_id="M2",
        accumulated_gross=first.accumulated_gross_pnl_usd,
    )
    context = TradeLogContext(
        signal={},
        provenance={},
        decision_quality={},
        mae_usd=Decimal("-1"),
        mfe_usd=Decimal("1"),
        risk_amount_usd=close.position.risk_amount_usd,
        entry_notional_usd=Decimal("70000"),
        data_quality_status="COMPLETE",
        data_quality_reasons=(),
        stopped=True,
        had_partial_close=True,
        traded_quantity=Decimal("1"),
    )

    trade = trade_log_from_close(
        close, trade_id=trade_id_for(close.position.position_id), context=context
    )

    assert trade["size"] == "1"
    assert "PARTIAL" in trade["outcome_tags"]
    # ``return_pct`` uses the immutable entry notional, not the residual
    # exposure a partial close leaves behind.
    assert trade["return_pct"] == return_percent(close.net_pnl_usd, Decimal("70000"))
    assert trade["return_pct"] != return_percent(close.net_pnl_usd, close.position.notional_usd)


def test_a_journal_context_that_contradicts_the_position_is_rejected() -> None:
    first = partial_close(_position(), _fill(), at=NOW, decision_id="M1")
    remaining = position_from_contract(first.payload)
    close = close_full(
        remaining,
        _fill(quantity="0.5", price="69900", fee="17.475"),
        at="2026-11-14T13:00:00.000Z",
        decision_id="M2",
        accumulated_gross=first.accumulated_gross_pnl_usd,
    )
    context = TradeLogContext(
        signal={},
        provenance={},
        decision_quality={},
        mae_usd=Decimal("-1"),
        mfe_usd=Decimal("1"),
        risk_amount_usd=Decimal("999"),
        entry_notional_usd=close.position.notional_usd,
        data_quality_status="COMPLETE",
        data_quality_reasons=(),
        stopped=True,
    )

    with pytest.raises(PositionError):
        trade_log_from_close(
            close, trade_id=trade_id_for(close.position.position_id), context=context
        )


def _valid_context(close: Any, **overrides: Any) -> TradeLogContext:
    values: dict[str, Any] = {
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
    return TradeLogContext(**values)


def _closed_position() -> Any:
    return close_full(
        _position(),
        _fill(side=OrderSide.SELL, price="69900", quantity="1", fee="17.475"),
        at=NOW,
        decision_id="M2",
    )


def _render(close: Any, context: TradeLogContext) -> dict[str, Any]:
    return trade_log_from_close(
        close, trade_id=trade_id_for(close.position.position_id), context=context
    )


def test_the_journal_context_rejects_implausible_mae_mfe_magnitudes() -> None:
    close = _closed_position()

    with pytest.raises(PositionError):
        _render(close, _valid_context(close, mae_usd=Decimal("-1e20")))
    with pytest.raises(PositionError):
        _render(close, _valid_context(close, mfe_usd=Decimal("1e20")))


def test_the_journal_context_rejects_a_traded_quantity_below_the_open_size() -> None:
    close = _closed_position()

    with pytest.raises(PositionError):
        _render(close, _valid_context(close, traded_quantity=Decimal("0.5")))
    with pytest.raises(PositionError):
        _render(close, _valid_context(close, traded_quantity=Decimal("1e20")))


def test_the_journal_context_rejects_mutually_exclusive_outcomes() -> None:
    close = _closed_position()

    with pytest.raises(PositionError):
        _render(close, _valid_context(close, stopped=True, target=True))
    with pytest.raises(PositionError):
        _render(close, _valid_context(close, stopped=False, manual=True))
    with pytest.raises(PositionError):
        _render(close, _valid_context(close, stopped=False, target=True))
