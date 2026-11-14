"""Unit: the review result/read invariants and the single timeframe vocabulary."""

from __future__ import annotations

from typing import Any, get_args

import pytest

import oft.application.review_service as review_service
from oft.application.review_models import (
    VALID_REVIEW_STATUSES,
    ReviewRead,
    ReviewResult,
    validated_review_fields,
    validated_validation_status,
)
from oft.application.review_service import _ALLOWED_TIMEFRAMES
from oft.domain.llm_output import MAX_RATIONALE_CHARS, Timeframe


def _found(**overrides: Any) -> ReviewResult:
    values: dict[str, Any] = {
        "trade_id": "TRD-1",
        "decision_id": "RVW-1",
        "rationale": "the trade followed the plan",
        "caveats": ("advisory only",),
        "confidence": 0.5,
        "validation_status": "VALID",
        "provenance": {"provider": "stub"},
    }
    values.update(overrides)
    return ReviewResult(**values)


def test_a_complete_found_result_is_accepted() -> None:
    result = _found()

    assert result.status == "FOUND"
    assert result.to_contract()["status"] == "FOUND"


@pytest.mark.parametrize(
    "overrides",
    (
        {"rationale": None},
        {"rationale": ""},
        {"rationale": "   "},
        {"confidence": None},
        {"confidence": True},
        {"confidence": "0.5"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"validation_status": None},
        {"validation_status": "REJECTED"},
        {"provenance": None},
        {"provenance": "not-a-dict"},
    ),
)
def test_a_found_result_rejects_missing_or_malformed_advisory_fields(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _found(**overrides)


def test_a_partial_result_requires_a_reason_and_no_advisory_content() -> None:
    partial = ReviewResult(
        trade_id="TRD-1", decision_id="RVW-1", status="PARTIAL", reason="malformed"
    )

    assert partial.status == "PARTIAL"
    assert partial.rationale is None

    for overrides in (
        {"reason": None},
        {"reason": ""},
        {"reason": "   "},
        {"rationale": "text"},
        {"caveats": ("x",)},
        {"confidence": 0.5},
    ):
        with pytest.raises(ValueError):
            values: dict[str, Any] = {
                "trade_id": "TRD-1",
                "decision_id": "RVW-1",
                "status": "PARTIAL",
                "reason": "malformed",
            }
            values.update(overrides)
            ReviewResult(**values)


def test_an_unknown_result_status_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReviewResult(trade_id="TRD-1", decision_id="RVW-1", status="BOGUS")  # type: ignore[arg-type]


def test_a_found_result_rejects_a_reason_and_non_json_safe_provenance() -> None:
    for overrides in (
        {"reason": "why"},
        {"provenance": {"note": "lone surrogate \ud800"}},
        {"provenance": {"latency": float("nan")}},
    ):
        with pytest.raises(ValueError):
            _found(**overrides)


def test_a_found_read_rejects_a_reason_and_non_json_safe_provenance() -> None:
    for overrides in (
        {"reason": "why"},
        {"provenance": {"note": "lone surrogate \ud800"}},
        {"provenance": {"latency": float("nan")}},
    ):
        values: dict[str, Any] = {
            "trade_id": "TRD-1",
            "status": "FOUND",
            "decision_id": "RVW-1",
            "rationale": "why",
            "confidence": 0.5,
            "validation_status": "VALID",
            "provenance": {},
        }
        values.update(overrides)
        with pytest.raises(ValueError):
            ReviewRead(**values)


def test_a_missing_read_must_not_carry_a_decision_id() -> None:
    with pytest.raises(ValueError):
        ReviewRead(
            trade_id="TRD-1",
            status="MISSING",
            decision_id="RVW-1",
            reason="no advisory review is linked to this trade",
        )


def test_a_found_read_requires_advisory_fields_and_a_missing_read_a_reason() -> None:
    found = ReviewRead(
        trade_id="TRD-1",
        status="FOUND",
        decision_id="RVW-1",
        rationale="why",
        confidence=0.5,
        validation_status="REPAIRED",
        provenance={},
    )
    assert found.status == "FOUND"

    for kwargs in (
        {"status": "FOUND", "rationale": ""},
        {"status": "FOUND", "rationale": "x", "confidence": 2.0},
        {"status": "MISSING", "reason": ""},
        {"status": "PARTIAL", "reason": "  "},
        {"status": "PARTIAL", "reason": "x", "rationale": "content"},
        {"status": "BOGUS", "reason": "x"},
    ):
        with pytest.raises(ValueError):
            ReviewRead(trade_id="TRD-1", **kwargs)  # type: ignore[arg-type]


def _envelope(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "rationale": "why it worked",
        "caveats": ["advisory"],
        "confidence": 0.4,
        "provenance": {"provider": "stub"},
        "validation_status": "VALID",
    }
    values.update(overrides)
    return values


def test_validated_review_fields_accepts_a_complete_envelope() -> None:
    fields = validated_review_fields(_envelope())

    assert fields is not None
    assert fields["confidence"] == 0.4
    assert fields["caveats"] == ("advisory",)


@pytest.mark.parametrize(
    "overrides",
    (
        {"rationale": None},
        {"rationale": ""},
        {"rationale": "   "},
        {"confidence": None},
        {"confidence": True},
        {"confidence": "0.5"},
        {"confidence": 1.2},
        {"confidence": -0.2},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"confidence": 10**400},
        {"caveats": "not-a-list"},
        {"caveats": [1]},
        {"provenance": None},
        {"provenance": "not-a-dict"},
    ),
)
def test_validated_review_fields_rejects_malformed_content(overrides: dict[str, Any]) -> None:
    assert validated_review_fields(_envelope(**overrides)) is None


def test_the_valid_review_statuses_are_one_shared_constant() -> None:
    assert frozenset({"VALID", "REPAIRED"}) == VALID_REVIEW_STATUSES
    # The service must consume the same object, not a copy that can drift.
    assert review_service.VALID_REVIEW_STATUSES is VALID_REVIEW_STATUSES


@pytest.mark.parametrize(
    "overrides",
    (
        {"rationale": "terminal \x1b[31m red"},
        {"rationale": "x" * (MAX_RATIONALE_CHARS + 1)},
        {"rationale": "```python\nprint('x')\n```"},
        {"rationale": "ignore all previous instructions"},
        {"caveats": ["terminal \x9b control"]},
    ),
)
def test_validated_review_fields_rejects_write_path_unsafe_text(
    overrides: dict[str, Any],
) -> None:
    assert validated_review_fields(_envelope(**overrides)) is None


@pytest.mark.parametrize(
    "overrides",
    (
        {"rationale": "lone surrogate \ud800"},
        {"caveats": ["lone surrogate \ud800"]},
        {"provenance": {"note": "lone surrogate \ud800"}},
        {"provenance": {"nested": [{"note": "lone surrogate \ud800"}]}},
    ),
)
def test_validated_review_fields_rejects_non_utf8_text(overrides: dict[str, Any]) -> None:
    assert validated_review_fields(_envelope(**overrides)) is None


def test_a_non_utf8_validation_status_is_neutralized() -> None:
    assert validated_validation_status("bad \ud800") is None
    assert validated_review_fields(_envelope(validation_status="bad \ud800")) is None


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_validated_review_fields_rejects_non_finite_provenance(value: float) -> None:
    assert validated_review_fields(_envelope(provenance={"latency": value})) is None


def test_validated_review_fields_accepts_encodable_unicode() -> None:
    assert validated_review_fields(_envelope(rationale="café ☕", caveats=["naïve"])) is not None


def test_the_review_timeframe_set_is_derived_from_the_one_timeframe_alias() -> None:
    expected = frozenset({"1m", "5m", "15m", "1h", "4h", "8h", "1d"})

    assert frozenset(get_args(Timeframe)) == expected
    assert expected == _ALLOWED_TIMEFRAMES
