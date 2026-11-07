"""Opportunity ledger entries and trigger events validate their contracts."""

from __future__ import annotations

from pathlib import Path

import _helpers
from _registry import validate


def test_opportunity_validates_and_condition_ids_are_code_assigned(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    payload = loop["opportunity"]

    validate("opportunity.schema.json", payload)
    assert [condition["condition_id"] for condition in payload["trigger_conditions"]] == ["C-1"]
    assert [entry["to_state"] for entry in payload["state_history"]] == ["OPEN", "TRIGGERED"]


def test_trigger_event_validates_and_is_code_sourced(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    trigger = loop["trigger"]

    validate("trigger-event.schema.json", trigger)
    assert trigger["trigger_source"] == "CODE"
    assert trigger["snapshot"]["snapshot_hash"]
    assert trigger["condition_results"][0]["evaluable"] is True
    assert trigger["condition_results"][0]["met"] is True
    assert trigger["idempotency_key"].startswith("TRG-")


def test_risk_verdict_payload_validates_through_the_common_definition(
    tmp_path: Path,
) -> None:
    from _registry import load
    from jsonschema import Draft202012Validator

    loop = _helpers.build_loop(tmp_path)
    verdict = loop["trigger"]["risk_verdict"]
    assert verdict is not None
    common = load("common.schema.json")
    validator = Draft202012Validator({"$ref": "#/$defs/riskVerdict", "$defs": common["$defs"]})
    errors = list(validator.iter_errors(verdict))
    assert not errors, [error.message for error in errors]
