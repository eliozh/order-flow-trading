"""Contract: management decisions, orders, positions, and trade logs stay valid."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import _helpers
import pytest
from _registry import load, validate
from jsonschema import Draft202012Validator

from oft.adapters.llm_stub import LLMStub
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementRejected, ManagementService
from oft.ports.llm import LLMRole

FIXTURES = (
    "management_hold.json",
    "management_partial_close.json",
    "management_full_close.json",
    "management_full_close_bad_stop.json",
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


def test_a_decision_provenance_may_carry_due_conditions(tmp_path: Path) -> None:
    """H3: the additive due-condition provenance is a valid envelope field."""
    state = _manage(tmp_path, "management_hold.json")
    container = state["loop"]["container"]
    decision = container.store.get_decision(state["result"].decision_id)
    assert decision is not None
    envelope = json.loads(decision.payload_json)
    envelope["provenance"]["due_conditions"] = ["CADENCE", "RISK_BREACH"]

    validate("llm-decision.schema.json", envelope)

    corrupted = json.loads(decision.payload_json)
    corrupted["provenance"]["due_conditions"] = ["NOT_A_CONDITION"]
    with pytest.raises(AssertionError):
        validate("llm-decision.schema.json", corrupted)


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


def _rewrite_position(container: Any, position_id: str, payload: dict[str, Any]) -> None:
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def test_a_rejected_management_envelope_validates_the_decision_contract(
    tmp_path: Path,
) -> None:
    """M8: the REJECTED envelope is a first-class llm-decision document."""
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=_helpers.LLM_FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_invalid_stop.json"},
        ),
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    )

    with pytest.raises(ManagementRejected) as excinfo:
        service.manage(position["position_id"], idempotency_key="contract-rejected")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    decision = container.store.list_decisions(role="MANAGEMENT")[-1]
    envelope = json.loads(decision.payload_json)
    assert envelope["validation_status"] == "REJECTED"
    assert envelope["validation_errors"]
    validate("llm-decision.schema.json", envelope)


def test_a_stopless_position_persists_a_contract_decimal_stop(tmp_path: Path) -> None:
    """M2: an absent stop serializes as the contract decimal "0", not "None"."""
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    position_id = position["position_id"]
    payload = json.loads(container.store.get_position(position_id).payload_json)
    payload["stop_price"] = None
    _rewrite_position(container, position_id, payload)
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=_helpers.LLM_FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    )

    result = service.manage(position_id, idempotency_key="contract-stopless")

    assert result.order is not None
    verdict = container.store.get_risk_verdict(result.order["risk_verdict_id"])
    assert verdict is not None
    requested = json.loads(verdict.payload_json)["requested"]
    assert requested["stop_price"] == "0"
    common = load("common.schema.json")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": common["$id"],
        "$defs": common["$defs"],
        "$ref": "#/$defs/riskRequest",
    }
    errors = list(Draft202012Validator(schema).iter_errors(requested))
    assert not errors, "\n".join(error.message for error in errors)
    after = json.loads(container.store.get_position(position_id).payload_json)
    assert after["stop_price"] is None
