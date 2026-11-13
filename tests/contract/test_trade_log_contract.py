"""Contract: a closed trade satisfies ``trade-log.schema.json`` exactly."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import _helpers
from _registry import validate

from oft.application.position_service import PositionService
from oft.domain.oms import Position, PositionSide
from position_support import set_position_mark


def _close(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        position["updated_at"],
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    result = service.monitor(position["position_id"])
    assert result.trade_log is not None
    return position, result.trade_log


def test_trade_log_validates_the_contract(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)
    validate("trade-log.schema.json", trade)


def test_trade_log_formula_holds_to_eight_decimal_places(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)

    gross = Decimal(trade["gross_pnl_usd"])
    fees = Decimal(trade["fees_usd"])
    funding = Decimal(trade["funding_usd"])
    net = Decimal(trade["net_pnl_usd"])
    assert net == (gross - fees + funding).quantize(Decimal("0.00000001"))
    assert trade["funding_usd"] == "0"
    assert "funding accrual not in this slice" in trade["data_quality"]["reasons"]


def test_trade_log_names_every_unmeasured_field(tmp_path: Path) -> None:
    _position, trade = _close(tmp_path)

    reasons = trade["data_quality"]["reasons"]
    assert any("MAE" in reason for reason in reasons)
    assert any("MFE" in reason for reason in reasons)
    assert any("R multiple" in reason for reason in reasons)
    assert trade["r_multiple"] is None
    assert trade["mae_usd"] == "0"
    assert trade["mfe_usd"] == "0"
    assert trade["signal"] == {
        "scan_id": None,
        "report_id": None,
        "opportunity_id": None,
        "trigger_id": None,
        "decision_id": None,
    }
    assert trade["outcome_tags"] == []
    assert trade["review_decision_id"] is None
    assert trade["decision_quality"]["matched_rules"] is None
    assert trade["decision_quality"]["confidence"] is None
    assert trade["provenance"]["llm_provider"] == "stub"
    assert trade["provenance"]["prompt_version"] >= 1


def test_closed_position_validates_the_position_contract(tmp_path: Path) -> None:
    position, _trade = _close(tmp_path)
    loop_payload = position
    assert loop_payload["state"] == "OPEN"  # the pre-close payload from the entry

    # Read the persisted row back and validate the closed contract document.
    database = Path(tmp_path) / "data" / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?",
            (position["position_id"],),
        ).fetchone()
    closed = json.loads(row[0])
    validate("position.schema.json", closed)
    assert closed["state"] == "CLOSED"
    assert closed["closed_at"] is not None
    assert closed["exit_price"] is not None
    assert closed["realized_pnl_usd"] is not None
    assert closed["unrealized_pnl_usd"] is None


def test_zero_fee_zero_net_close_serializes_money_as_plain_decimals() -> None:
    """An exact zero must be "0", never the exponent form that fails `money`."""
    position = Position(
        position_id="POS-ZERO",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        state="CLOSED",
        size=Decimal("0.01"),
        entry_price=Decimal("70000"),
        avg_entry_price=Decimal("70000"),
        mark_price=Decimal("70000"),
        notional_usd=Decimal("700.00"),
        stop_price=Decimal("69000"),
        target_prices=(),
        leverage=2.0,
        margin_usd=Decimal("350.00"),
        risk_amount_usd=Decimal("10.00"),
        risk_pct=0.8,
        fees_usd=Decimal("0").quantize(Decimal("0.00000001")),
        opened_at="2026-11-14T11:00:00.000Z",
        updated_at="2026-11-14T12:00:00.000Z",
        source_trigger_id="TRG-TEST",
        closed_at="2026-11-14T12:00:00.000Z",
        exit_price=Decimal("70000"),
        realized_pnl_usd=Decimal("0").quantize(Decimal("0.00000001")),
    )

    payload = position.to_contract()

    validate("position.schema.json", payload)
    assert payload["fees_usd"] == "0"
    assert payload["funding_usd"] == "0"
    assert payload["realized_pnl_usd"] == "0"
    assert "E" not in payload["fees_usd"]
    assert "E" not in payload["funding_usd"]
    assert "E" not in payload["realized_pnl_usd"]
