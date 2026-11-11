"""Paper orders, fills, and positions satisfy their contracts."""

from __future__ import annotations

import json
from pathlib import Path

import _helpers
from _registry import load, validate
from jsonschema import Draft202012Validator


def test_order_fill_and_position_validate(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    result = loop["result"]

    assert result.order is not None
    assert result.fill is not None
    assert result.position is not None
    validate("order.schema.json", result.order)
    validate("fill.schema.json", result.fill)
    validate("position.schema.json", result.position)


def test_order_has_a_deterministic_client_id_and_risk_verdict(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    order = loop["result"].order
    assert order is not None

    assert order["environment"] == "PAPER"
    assert order["client_order_id"].startswith("oft-paper-BTCUSDT-entry-")
    assert order["risk_verdict_id"]
    assert order["approval_id"]
    assert order["status"] == "FILLED"


def test_paper_records_are_the_same_values_the_store_persists(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    order = loop["result"].order

    stored = container.store.list_orders()
    assert len(stored) == 1
    assert json.loads(stored[0].payload_json) == order


def test_risk_verdict_satisfies_the_common_contract(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    verdict = loop["result"].risk_verdict
    _assert_risk_verdict_contract(verdict)
    assert verdict["verdict"] in {"ALLOW", "REDUCE"}
    assert isinstance(verdict["allowed"]["quantity"], str)
    assert "spread_bps" in verdict["stale_inputs"]
    assert "daily_loss_usd" in verdict["stale_inputs"]


def test_a_block_verdict_satisfies_the_common_contract() -> None:
    from decimal import Decimal

    from oft.domain.risk import DEFAULT_CAPS, RiskRequest, evaluate_risk, verdict_to_contract

    verdict = evaluate_risk(
        request=RiskRequest(
            symbol="BTCUSDT",
            side="LONG",
            entry_price=Decimal("70000.00"),
            stop_price=None,
            quantity=Decimal("0.01"),
            risk_pct=0.8,
            leverage=2.0,
            notional_usd=None,
        ),
        caps=DEFAULT_CAPS,
        equity=Decimal("10000.00"),
        open_positions=0,
        daily_loss_usd=None,
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T11:59:30.000Z",
        now="2026-11-06T12:00:00.000Z",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5.00"),
    )
    payload = verdict_to_contract(
        verdict,
        requested_payload={
            "entry_price": "70000.00",
            "stop_price": "0",
            "quantity": "0.01",
            "risk_pct": 0.8,
            "leverage": 2.0,
            "notional_usd": None,
        },
    )

    _assert_risk_verdict_contract(payload)
    assert payload["verdict"] == "BLOCK"
    assert payload["allowed"]["quantity"] is None
    assert payload["allowed"]["notional_usd"] is None


def _assert_risk_verdict_contract(verdict: dict) -> None:
    common = load("common.schema.json")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": common["$id"],
        "$defs": common["$defs"],
        "$ref": "#/$defs/riskVerdict",
    }

    errors = list(Draft202012Validator(schema).iter_errors(verdict))

    assert not errors, "\n".join(error.message for error in errors)
