"""Paper orders, fills, and positions satisfy their contracts."""

from __future__ import annotations

import json
from pathlib import Path

import _helpers
from _registry import validate


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
