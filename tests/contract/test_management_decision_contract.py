"""Contract: management decisions, orders, positions, and trade logs stay valid."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import _helpers
from _registry import validate

from oft.adapters.llm_stub import LLMStub
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.ports.llm import LLMRole

FIXTURES = (
    "management_hold.json",
    "management_partial_close.json",
    "management_full_close.json",
    "management_add.json",
    "management_oversized_add.json",
    "management_invalid_stop.json",
    "management_loosen_stop.json",
    "management_tighten_stop.json",
)


def test_every_management_fixture_satisfies_the_provider_schema() -> None:
    from jsonschema import Draft202012Validator

    from oft.application.llm_validation import canonical_schema

    validator = Draft202012Validator(canonical_schema())
    for name in FIXTURES:
        document = json.loads((_helpers.LLM_FIXTURES / name).read_text(encoding="utf-8"))
        errors = list(validator.iter_errors(document))
        assert not errors, f"{name}: {[error.message for error in errors]}"


def _manage(tmp_path: Path, fixture: str) -> dict[str, Any]:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=_helpers.LLM_FIXTURES,
            overrides={LLMRole.MANAGEMENT: fixture},
        ),
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    )
    result = service.manage(position["position_id"], idempotency_key=f"contract-{fixture}")
    return {"loop": loop, "result": result, "position": position}


def test_a_hold_decision_and_position_validate_the_contracts(tmp_path: Path) -> None:
    state = _manage(tmp_path, "management_hold.json")
    container = state["loop"]["container"]

    decision = container.store.get_decision(state["result"].decision_id)
    assert decision is not None
    envelope = json.loads(decision.payload_json)
    validate("llm-decision.schema.json", envelope)
    record = container.store.get_position(state["position"]["position_id"])
    assert record is not None
    validate("position.schema.json", json.loads(record.payload_json))
    assert envelope["position_management"]["action"] == "HOLD"


def test_a_partial_close_order_and_position_validate_the_contracts(tmp_path: Path) -> None:
    state = _manage(tmp_path, "management_partial_close.json")
    container = state["loop"]["container"]

    result = state["result"]
    assert result.order is not None and result.fill is not None
    validate("order.schema.json", result.order)
    validate("fill.schema.json", result.fill)
    record = container.store.get_position(state["position"]["position_id"])
    assert record is not None
    validate("position.schema.json", json.loads(record.payload_json))
    decision = container.store.get_decision(result.decision_id)
    assert decision is not None
    validate("llm-decision.schema.json", json.loads(decision.payload_json))


def test_a_full_close_order_fill_and_trade_log_validate_the_contracts(
    tmp_path: Path,
) -> None:
    state = _manage(tmp_path, "management_full_close.json")
    container = state["loop"]["container"]
    result = state["result"]

    assert result.order is not None and result.fill is not None
    validate("order.schema.json", result.order)
    validate("fill.schema.json", result.fill)
    trade = container.store.get_trade_log_for_position(state["position"]["position_id"])
    assert trade is not None
    validate("trade-log.schema.json", json.loads(trade.payload_json))
    record = container.store.get_position(state["position"]["position_id"])
    assert record is not None
    validate("position.schema.json", json.loads(record.payload_json))


def test_an_add_order_and_position_validate_the_contracts(tmp_path: Path) -> None:
    state = _manage(tmp_path, "management_add.json")
    container = state["loop"]["container"]
    result = state["result"]

    assert result.order is not None and result.fill is not None
    validate("order.schema.json", result.order)
    validate("fill.schema.json", result.fill)
    record = container.store.get_position(state["position"]["position_id"])
    assert record is not None
    validate("position.schema.json", json.loads(record.payload_json))
    assert result.risk is not None
    assert result.risk["requested"]["quantity"]
    assert result.risk["allowed"]["quantity"]


def test_decision_provenance_is_complete_and_never_contains_a_raw_prompt(
    tmp_path: Path,
) -> None:
    state = _manage(tmp_path, "management_hold.json")
    container = state["loop"]["container"]
    decision = container.store.get_decision(state["result"].decision_id)
    assert decision is not None
    raw = decision.payload_json
    envelope = json.loads(raw)
    provenance = envelope["provenance"]

    assert provenance["provider"] == "stub"
    assert provenance["model"] == "fixture"
    assert provenance["prompt_id"] == "management"
    assert provenance["prompt_version"] >= 1
    assert provenance["prompt_hash"]
    assert "knowledge_artifact_hash" in provenance
    assert set(provenance["cost"]) >= {"amount_usd", "currency", "price_version", "unpriced"}
    assert "system_prompt" not in raw
    assert "user_prompt" not in raw
    assert "You are an order-flow position-management assistant" not in raw
