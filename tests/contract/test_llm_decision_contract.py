"""Persisted decisions and per-call budget rows validate their contracts."""

from __future__ import annotations

import json
from pathlib import Path

import _helpers
from _registry import validate


def test_valid_llm_fixtures_validate_against_the_provider_schema() -> None:
    from oft.application.llm_validation import canonical_schema

    schema = canonical_schema()
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    for name in (
        "analysis_valid.json",
        "opportunity_valid.json",
        "entry_recheck_enter_long.json",
        "entry_recheck_no_entry.json",
        "entry_recheck_stopless.json",
        "entry_recheck_oversized.json",
        "entry_recheck_far_mark.json",
    ):
        document = json.loads((_helpers.LLM_FIXTURES / name).read_text(encoding="utf-8"))
        errors = list(validator.iter_errors(document))
        assert not errors, f"{name}: {[error.message for error in errors]}"


def test_invalid_llm_fixtures_fail_the_provider_schema() -> None:
    from jsonschema import Draft202012Validator

    from oft.application.llm_validation import canonical_schema

    validator = Draft202012Validator(canonical_schema())
    for name in (
        "invalid_schema.json",
        "refusal.json",
        "truncated.json",
        "entry_recheck_far_mark_limit.json",
    ):
        document = json.loads((_helpers.LLM_FIXTURES / name).read_text(encoding="utf-8"))
        assert list(validator.iter_errors(document)), name


def test_persisted_analysis_and_entry_decisions_validate(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    for decision in container.store.list_decisions():
        validate("llm-decision.schema.json", json.loads(decision.payload_json))


def test_every_budget_record_validates(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)

    assert loop["budget_records"]
    for record in loop["budget_records"]:
        validate("budget-record.schema.json", record)
        assert record["unpriced"] is True
        assert record["provider"] == "stub"
        assert record["model"] == "fixture"
