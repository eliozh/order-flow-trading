"""Opportunity lifecycle tests: code-assigned IDs, validation, append-only history."""

from __future__ import annotations

from decimal import Decimal

import pytest

from oft.domain.opportunities import (
    ConditionDraft,
    ConditionDraftError,
    Opportunity,
    OpportunityState,
    OpportunityTransitionError,
    assign_conditions,
    new_opportunity,
    transition,
    validate_condition_drafts,
)


def _draft(**overrides: object) -> ConditionDraft:
    payload: dict[str, object] = {
        "draft_id": "c1",
        "type": "PRICE_TOUCH_ZONE",
        "params": {
            "price": None,
            "price_low": "69900.00",
            "price_high": "70100.00",
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "retest of the range high",
        },
        "window": {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"},
        "required_data": ("MARK_PRICE",),
        "description": "price touches the zone",
    }
    payload.update(overrides)
    return ConditionDraft(**payload)  # type: ignore[arg-type]


def _opportunity() -> Opportunity:
    return new_opportunity(
        opportunity_id="OPP-BTCUSDT-1",
        symbol="BTCUSDT",
        side="LONG",
        zone_low=Decimal("69000.00"),
        zone_high=Decimal("71000.00"),
        entry_preference="MARKET",
        trigger_conditions=assign_conditions((_draft(),)),
        invalidation_conditions=(),
        expires_at="2026-12-01T00:00:00.000Z",
        thesis="range high retest",
        confidence=0.72,
        risk_request={"entry_price": "70000.00", "stop_price": "69000.00"},
        source={"scan_id": "SCAN-1", "report_id": "RPT-1", "decision_id": "DEC-1"},
        provenance={"llm": {}, "calc_snapshot_id": "SNAP-1", "calc_snapshot_hash": "h"},
        data_quality={"status": "COMPLETE", "reasons": [], "gaps": []},
        at="2026-11-06T12:00:00.000Z",
    )


def test_condition_ids_are_code_assigned_not_model_supplied() -> None:
    conditions = assign_conditions((_draft(draft_id="model-local-1"),))

    assert [condition.condition_id for condition in conditions] == ["C-1"]
    assert conditions[0].draft_id == "model-local-1"
    assert conditions[0].evaluable is True


def test_invalid_zone_order_rejects_the_draft() -> None:
    draft = _draft(
        params={
            "price": None,
            "price_low": "70100.00",
            "price_high": "69900.00",
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "",
        }
    )

    with pytest.raises(ConditionDraftError) as excinfo:
        validate_condition_drafts((draft,))

    assert "price_low" in str(excinfo.value)
    assert excinfo.value.code == "VALIDATION_FAILED"


def test_missing_required_zone_parameter_rejects_the_draft() -> None:
    draft = _draft(
        params={
            "price": None,
            "price_low": None,
            "price_high": "70100.00",
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "",
        }
    )

    with pytest.raises(ConditionDraftError):
        validate_condition_drafts((draft,))


def test_unsupported_condition_type_is_rejected() -> None:
    with pytest.raises(ConditionDraftError):
        validate_condition_drafts((_draft(type="CVD_ABOVE"),))


def test_non_numeric_zone_parameter_is_rejected() -> None:
    draft = _draft(
        params={
            "price": None,
            "price_low": "not-a-number",
            "price_high": "70100.00",
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "",
        }
    )

    with pytest.raises(ConditionDraftError):
        validate_condition_drafts((draft,))


def test_non_positive_zone_parameter_is_rejected() -> None:
    draft = _draft(
        params={
            "price": None,
            "price_low": "0",
            "price_high": "70100.00",
            "threshold": None,
            "unit": "PRICE",
            "comparator": "BETWEEN",
            "lookback_bars": None,
            "session_states": [],
            "note": "",
        }
    )

    with pytest.raises(ConditionDraftError):
        validate_condition_drafts((draft,))


def test_state_history_is_append_only_and_transitions_are_code_only() -> None:
    opportunity = _opportunity()
    triggered = transition(
        opportunity,
        OpportunityState.TRIGGERED,
        at="2026-11-06T12:05:00.000Z",
        reason_code="TRIGGER_MET",
        note="trigger-1",
    )

    assert triggered.state == OpportunityState.TRIGGERED
    assert len(opportunity.state_history) == 1
    assert len(triggered.state_history) == 2
    assert triggered.state_history[-1].from_state == OpportunityState.OPEN
    assert triggered.state_history[-1].to_state == OpportunityState.TRIGGERED
    assert triggered.state_history[-1].reason_code == "TRIGGER_MET"


@pytest.mark.parametrize(
    "target",
    [OpportunityState.EXPIRED, OpportunityState.INVALIDATED],
)
def test_open_terminal_transitions(target: OpportunityState) -> None:
    updated = transition(
        _opportunity(),
        target,
        at="2026-11-06T12:05:00.000Z",
        reason_code="EXPIRED" if target is OpportunityState.EXPIRED else "REJECTED",
        note="reason",
    )
    assert updated.state is target


def test_terminal_states_cannot_transition_again() -> None:
    expired = transition(
        _opportunity(),
        OpportunityState.EXPIRED,
        at="2026-11-06T12:05:00.000Z",
        reason_code="EXPIRED",
        note="",
    )

    with pytest.raises(OpportunityTransitionError):
        transition(
            expired,
            OpportunityState.OPEN,
            at="2026-11-06T12:06:00.000Z",
            reason_code="REOPEN",
            note="",
        )


def test_transition_to_same_state_is_rejected() -> None:
    with pytest.raises(OpportunityTransitionError):
        transition(
            _opportunity(),
            OpportunityState.OPEN,
            at="2026-11-06T12:05:00.000Z",
            reason_code="NOOP",
            note="",
        )


def test_expiry_is_utc_and_enforced() -> None:
    opportunity = _opportunity()

    assert opportunity.is_expired("2026-11-30T00:00:00.000Z") is False
    assert opportunity.is_expired("2026-12-01T00:00:00.000Z") is True
    assert opportunity.is_expired("2026-12-02T00:00:00.000Z") is True


def test_new_opportunity_rejects_an_inverted_zone() -> None:
    with pytest.raises(ConditionDraftError):
        new_opportunity(
            opportunity_id="OPP-1",
            symbol="BTCUSDT",
            side="LONG",
            zone_low=Decimal("71000.00"),
            zone_high=Decimal("69000.00"),
            entry_preference="MARKET",
            trigger_conditions=(),
            invalidation_conditions=(),
            expires_at="2026-12-01T00:00:00.000Z",
            thesis="",
            confidence=0.5,
            risk_request={},
            source={},
            provenance={},
            data_quality={},
            at="2026-11-06T12:00:00.000Z",
        )


def test_new_opportunity_rejects_an_out_of_range_confidence() -> None:
    with pytest.raises(ConditionDraftError):
        new_opportunity(
            opportunity_id="OPP-1",
            symbol="BTCUSDT",
            side="LONG",
            zone_low=Decimal("69000.00"),
            zone_high=Decimal("71000.00"),
            entry_preference="MARKET",
            trigger_conditions=(),
            invalidation_conditions=(),
            expires_at="2026-12-01T00:00:00.000Z",
            thesis="",
            confidence=1.5,
            risk_request={},
            source={},
            provenance={},
            data_quality={},
            at="2026-11-06T12:00:00.000Z",
        )
