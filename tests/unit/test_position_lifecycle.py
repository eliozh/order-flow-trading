"""Position lifecycle: stop-touch symmetry and a full-close payload."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.domain.oms import Fill, OrderSide, Position, PositionSide
from oft.domain.pnl import net_pnl_usd
from oft.domain.positions import (
    FULL_CLOSE,
    FUNDING_REASON,
    PositionError,
    close_full,
    evaluate_protective_exit,
    management_decision_id,
    position_from_contract,
    trade_id_for,
    trade_log_from_close,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
NOW = "2026-11-14T12:00:00.000Z"
DECISION_ID = management_decision_id("POS-TEST", Decimal("68980"), NOW)


def _position(
    *,
    side: PositionSide = PositionSide.LONG,
    entry: str = "70000",
    stop: str | None = "69000",
    size: str = "0.01",
    fees: str = "0.35",
    state: str = "OPEN",
) -> Position:
    return Position(
        position_id="POS-TEST",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=side,
        state=state,
        size=Decimal(size),
        entry_price=Decimal(entry),
        avg_entry_price=Decimal(entry),
        mark_price=Decimal(entry),
        notional_usd=Decimal(entry) * Decimal(size),
        stop_price=None if stop is None else Decimal(stop),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("350"),
        risk_amount_usd=Decimal("10"),
        risk_pct=0.8,
        fees_usd=Decimal(fees),
        opened_at="2026-11-14T11:00:00.000Z",
        updated_at="2026-11-14T11:00:00.000Z",
        source_trigger_id="TRG-TEST",
    )


def _fill(price: str = "68980", fee: str = "0.345") -> Fill:
    return Fill(
        fill_id="FIL-EXIT",
        order_id="ORD-EXIT",
        environment="PAPER",
        account_id="ACC-PAPER",
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        price=Decimal(price),
        quantity=Decimal("0.01"),
        quote_quantity=Decimal(price) * Decimal("0.01"),
        fee=Decimal(fee),
        fee_asset="USDT",
        maker=False,
        filled_at=NOW,
        received_at=NOW,
    )


def _registry() -> Registry:
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate(name: str, payload: dict[str, Any]) -> None:
    document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_long_stop_is_touched_at_or_below_the_stop() -> None:
    position = _position()
    exit_ = evaluate_protective_exit(position, Decimal("69000"), at=NOW)
    assert exit_ is not None
    assert exit_.position_id == "POS-TEST"
    assert exit_.mark_price == Decimal("69000")
    assert exit_.rule == "STOP_TOUCH"


def test_long_stop_above_the_stop_produces_no_decision() -> None:
    assert evaluate_protective_exit(_position(), Decimal("69000.01"), at=NOW) is None


def test_short_stop_touch_is_symmetric() -> None:
    position = _position(side=PositionSide.SHORT, entry="70000", stop="71000")
    assert evaluate_protective_exit(position, Decimal("71000"), at=NOW) is not None
    assert evaluate_protective_exit(position, Decimal("71000.01"), at=NOW) is not None
    assert evaluate_protective_exit(position, Decimal("70999.99"), at=NOW) is None


def test_no_stop_means_no_protective_exit() -> None:
    assert evaluate_protective_exit(_position(stop=None), Decimal("60000"), at=NOW) is None


def test_stop_on_the_wrong_side_fails_closed() -> None:
    with pytest.raises(PositionError) as excinfo:
        evaluate_protective_exit(_position(stop="71000"), Decimal("70000"), at=NOW)
    assert excinfo.value.code == "POSITION_STATE_INVALID"


def test_management_decision_id_is_deterministic_and_input_bound() -> None:
    first = management_decision_id("POS-TEST", Decimal("68980"), NOW)
    second = management_decision_id("POS-TEST", Decimal("68980"), NOW)
    other_mark = management_decision_id("POS-TEST", Decimal("68981"), NOW)
    other_time = management_decision_id("POS-TEST", Decimal("68980"), "2026-11-14T12:00:01.000Z")
    assert first == second
    assert other_mark != first
    assert other_time != first


def test_close_full_writes_the_caller_decision_id_once() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id=DECISION_ID)

    assert close.event.decision_id == DECISION_ID
    history = close.to_contract()["management_history"]
    assert history[-1]["decision_id"] == DECISION_ID
    assert history[-1]["action"] == FULL_CLOSE
    assert history[-1]["executed_order_id"] == "ORD-EXIT"


def test_close_result_rejects_a_net_that_contradicts_its_terms() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id=DECISION_ID)

    with pytest.raises(ValueError):
        replace(close, net_pnl_usd=close.net_pnl_usd + Decimal("1"))


def test_a_non_finite_recorded_value_is_unavailable_not_a_crash() -> None:
    payload = _position().to_contract()
    payload["mark_price"] = "NaN"
    with pytest.raises(PositionError) as excinfo:
        position_from_contract(payload)
    assert excinfo.value.code == "DATA_UNAVAILABLE"

    payload = _position().to_contract()
    payload["size"] = "Infinity"
    with pytest.raises(PositionError) as excinfo:
        position_from_contract(payload)
    assert excinfo.value.code == "DATA_UNAVAILABLE"

    payload = _position().to_contract()
    payload["stop_price"] = "NaN"
    with pytest.raises(PositionError) as excinfo:
        position_from_contract(payload)
    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_close_full_produces_a_valid_closed_position_payload() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id=DECISION_ID)
    payload = close.to_contract()

    _validate("position.schema.json", payload)
    assert payload["state"] == "CLOSED"
    assert payload["closed_at"] == NOW
    assert payload["exit_price"] == "68980"
    # gross = (68980 - 70000) * 0.01 = -10.2
    assert close.gross_pnl_usd == Decimal("-10.20000000")
    # fees = entry 0.35 + exit 0.345 = 0.695
    assert close.fees_usd == Decimal("0.69500000")
    assert close.funding_usd == Decimal("0")
    assert close.net_pnl_usd == Decimal("-10.89500000")
    assert payload["realized_pnl_usd"] == "-10.89500000"
    assert payload["unrealized_pnl_usd"] is None
    assert payload["version"] == 2
    assert payload["management_history"][-1]["action"] == "FULL_CLOSE"
    assert payload["management_history"][-1]["executed_order_id"] == "ORD-EXIT"


def test_close_full_round_trips_through_the_contract_payload() -> None:
    open_position = _position()
    assert position_from_contract(open_position.to_contract()) == open_position

    close = close_full(open_position, _fill(), at=NOW, decision_id=DECISION_ID)
    rebuilt = position_from_contract(close.to_contract())
    assert rebuilt.state == "CLOSED"
    assert rebuilt.realized_pnl_usd == close.net_pnl_usd


def test_close_full_rejects_a_wrong_side_or_partial_fill() -> None:
    def _sided(side: OrderSide, quantity: str) -> Fill:
        return Fill(
            fill_id="FIL-X",
            order_id="ORD-X",
            environment="PAPER",
            account_id="ACC-PAPER",
            symbol="BTCUSDT",
            side=side,
            price=Decimal("68980"),
            quantity=Decimal(quantity),
            quote_quantity=Decimal("689.8"),
            fee=Decimal("0.345"),
            fee_asset="USDT",
            maker=False,
            filled_at=NOW,
            received_at=NOW,
        )

    with pytest.raises(PositionError):
        close_full(_position(), _sided(OrderSide.BUY, "0.01"), at=NOW, decision_id=DECISION_ID)
    with pytest.raises(PositionError):
        close_full(_position(), _sided(OrderSide.SELL, "0.005"), at=NOW, decision_id=DECISION_ID)


def test_trade_log_from_close_validates_and_holds_the_pnl_formula() -> None:
    close = close_full(_position(), _fill(), at=NOW, decision_id=DECISION_ID)
    trade = trade_log_from_close(close, trade_id=trade_id_for("POS-TEST"))

    _validate("trade-log.schema.json", trade)
    assert trade["funding_usd"] == "0"
    assert trade["gross_pnl_usd"] == "-10.20000000"
    assert trade["fees_usd"] == "0.69500000"
    assert trade["net_pnl_usd"] == "-10.89500000"
    assert Decimal(trade["net_pnl_usd"]) == (
        Decimal(trade["gross_pnl_usd"]) - Decimal(trade["fees_usd"]) + Decimal(trade["funding_usd"])
    )
    assert FUNDING_REASON in trade["data_quality"]["reasons"]
    assert trade["r_multiple"] is None
    assert trade["signal"] == {
        "scan_id": None,
        "report_id": None,
        "opportunity_id": None,
        "trigger_id": None,
        "decision_id": None,
    }
    assert trade["provenance"]["llm_provider"] == "stub"


def test_trade_log_emits_the_actual_funding_term() -> None:
    """Funding is serialized from the close, not hardcoded to "0"."""
    close = close_full(_position(), _fill(), at=NOW, decision_id=DECISION_ID)
    funding = Decimal("-0.05")
    funded = replace(
        close,
        funding_usd=funding,
        net_pnl_usd=net_pnl_usd(close.gross_pnl_usd, close.fees_usd, funding),
    )

    trade = trade_log_from_close(funded, trade_id=trade_id_for("POS-TEST"))

    _validate("trade-log.schema.json", trade)
    assert trade["funding_usd"] == "-0.05"
    assert Decimal(trade["net_pnl_usd"]) == (
        Decimal(trade["gross_pnl_usd"]) - Decimal(trade["fees_usd"]) + funding
    )
