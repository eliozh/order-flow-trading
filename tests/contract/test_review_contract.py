"""Contract: the additive REVIEW enum keeps every existing fixture valid."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import _helpers
import _registry
from jsonschema import Draft202012Validator

from oft.application.llm_validation import canonical_schema, validate_output
from oft.application.position_service import PositionService
from oft.application.review_service import ReviewService
from oft.domain.llm_output import Timeframe
from position_support import set_position_mark

VALID_FIXTURES = (
    "analysis_valid.json",
    "opportunity_valid.json",
    "entry_recheck_enter_long.json",
    "entry_recheck_no_entry.json",
    "entry_recheck_stopless.json",
    "entry_recheck_oversized.json",
    "entry_recheck_far_mark.json",
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
_ROLE_BY_DECISION = {
    "MARKET_ANALYSIS": "ANALYSIS",
    "OPPORTUNITY_PROPOSAL": "ANALYSIS",
    "ENTRY_RECHECK": "ENTRY_RECHECK",
    "POSITION_MANAGEMENT": "MANAGEMENT",
    "REVIEW": "REVIEW",
}
_ORIGINAL_REQUIRED = [
    "decision_type",
    "subject",
    "confidence",
    "rationale",
    "caveats",
    "analysis",
    "opportunity",
    "entry_recheck",
    "position_management",
]


def _validator() -> Draft202012Validator:
    return Draft202012Validator(canonical_schema())


def test_every_existing_provider_fixture_still_validates() -> None:
    validator = _validator()
    for name in VALID_FIXTURES:
        document = json.loads((_helpers.LLM_FIXTURES / name).read_text(encoding="utf-8"))
        errors = list(validator.iter_errors(document))
        assert not errors, f"{name}: {[error.message for error in errors]}"
        outcome = validate_output(
            json.dumps(document), role=_ROLE_BY_DECISION[document["decision_type"]]
        )
        assert outcome.status == "VALID", (name, outcome.errors)


def test_the_review_fixture_validates_and_is_semantically_advisory() -> None:
    document = json.loads((_helpers.LLM_FIXTURES / "review_valid.json").read_text(encoding="utf-8"))

    assert not list(_validator().iter_errors(document))
    outcome = validate_output(json.dumps(document), role="REVIEW")
    assert outcome.status == "VALID"
    assert outcome.output is not None
    assert outcome.output.decision_type == "REVIEW"
    assert all(
        getattr(outcome.output, field) is None
        for field in ("analysis", "opportunity", "entry_recheck", "position_management")
    )


def test_the_enum_change_is_additive_and_closed() -> None:
    output_schema = canonical_schema()
    decision_schema = _registry.load("llm-decision.schema.json")

    assert "REVIEW" in output_schema["properties"]["decision_type"]["enum"]
    assert "REVIEW" in decision_schema["properties"]["decision_type"]["enum"]
    assert {
        "MARKET_ANALYSIS",
        "OPPORTUNITY_PROPOSAL",
        "ENTRY_RECHECK",
        "POSITION_MANAGEMENT",
    } <= set(output_schema["properties"]["decision_type"]["enum"])
    assert output_schema["additionalProperties"] is False
    assert output_schema["required"] == _ORIGINAL_REQUIRED
    assert decision_schema["additionalProperties"] is False
    # No executable field was added by the REVIEW amendment.
    assert set(output_schema["properties"]) == set(_ORIGINAL_REQUIRED)
    assert "order" not in output_schema["properties"]
    assert "action" not in output_schema["properties"]


def test_the_schema_timeframe_enum_is_bound_to_the_python_alias() -> None:
    schema = canonical_schema()
    enum = schema["$defs"]["subject"]["properties"]["timeframe"]["enum"]

    assert enum == list(get_args(Timeframe))


def test_an_unknown_top_level_field_is_still_rejected() -> None:
    document = json.loads((_helpers.LLM_FIXTURES / "review_valid.json").read_text(encoding="utf-8"))
    document["apply_rule"] = True

    assert list(_validator().iter_errors(document))


def test_a_persisted_review_decision_validates_the_decision_contract(
    tmp_path: Path,
) -> None:
    loop = _helpers.build_loop(tmp_path)
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(container, position["position_id"], position["stop_price"])
    closed = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    ).monitor(position["position_id"])
    assert closed.trade_log is not None
    trade_id = closed.trade_log["trade_id"]
    service = ReviewService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
        provider=container.llm_provider,
        runtime=container.llm_runtime,
    )

    result = service.review(trade_id, idempotency_key="contract-review")

    decision = container.store.get_decision(result.decision_id)
    assert decision is not None
    envelope = json.loads(decision.payload_json)
    _registry.validate("llm-decision.schema.json", envelope)
    assert envelope["role"] == "REVIEW"
    assert envelope["decision_type"] == "REVIEW"
    assert envelope["subject"]["position_id"] == position["position_id"]
    assert envelope["provenance"]["prompt_id"] == "review"
    assert envelope["provenance"]["prompt_version"] == 1
    assert envelope["provenance"]["prompt_hash"]
    assert envelope["validation_status"] == "VALID"
    assert envelope["analysis"] is None
    assert envelope["opportunity"] is None
    assert envelope["entry_recheck"] is None
    assert envelope["position_management"] is None
