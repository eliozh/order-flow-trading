"""Structured opportunity ledger and the code-only state machine (ADR-0006).

Only the deterministic trigger evaluator (code) may move an opportunity out of
``OPEN``. The LLM supplies drafts; code assigns condition IDs, validates the
required parameters, and appends immutable history entries.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from oft.domain.conditions import (
    ConditionParamsError,
    condition_spec,
    contract_required_data,
    creation_evaluable,
    normalize_timestamp,
    validate_draft,
)

OPEN = "OPEN"


class OpportunityState(StrEnum):
    OPEN = "OPEN"
    TRIGGERED = "TRIGGERED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


_TERMINAL_STATES = frozenset(
    {OpportunityState.TRIGGERED, OpportunityState.EXPIRED, OpportunityState.INVALIDATED}
)

# TRIGGERED stays terminal for expiry, but a human reject or a NO_ENTRY re-check
# invalidates the trade idea, so that one transition is allowed.
_ALLOWED_TRANSITIONS: dict[OpportunityState, frozenset[OpportunityState]] = {
    OpportunityState.OPEN: frozenset(
        {OpportunityState.TRIGGERED, OpportunityState.EXPIRED, OpportunityState.INVALIDATED}
    ),
    OpportunityState.TRIGGERED: frozenset({OpportunityState.INVALIDATED}),
}


class OpportunityTransitionError(Exception):
    """The requested state change is not allowed by the state machine."""

    code = "POSITION_STATE_INVALID"


class ConditionDraftError(ConditionParamsError):
    """A draft condition is unsupported or missing required parameters."""


@dataclass(frozen=True)
class ConditionDraft:
    draft_id: str
    type: str
    params: Mapping[str, Any]
    window: Mapping[str, str]
    required_data: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class Condition:
    condition_id: str
    draft_id: str
    type: str
    params: Mapping[str, Any]
    window_start: str
    window_end: str
    required_data: tuple[str, ...]
    evaluable: bool
    unavailable_reason: str | None = None
    last_value: str | None = None
    last_evaluated_at: str | None = None
    last_result: bool | None = None
    description: str = ""

    def to_contract(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "type": self.type,
            "params": dict(self.params),
            "window": {"start": self.window_start, "end": self.window_end},
            "required_data": list(self.required_data),
            "evaluable": self.evaluable,
            "unavailable_reason": self.unavailable_reason,
            "last_value": self.last_value,
            "last_evaluated_at": self.last_evaluated_at,
            "last_result": self.last_result,
        }


@dataclass(frozen=True)
class StateHistoryEntry:
    from_state: OpportunityState | None
    to_state: OpportunityState
    at: str
    reason_code: str
    note: str

    def to_contract(self) -> dict[str, Any]:
        return {
            "from_state": None if self.from_state is None else self.from_state.value,
            "to_state": self.to_state.value,
            "at": self.at,
            "reason_code": self.reason_code,
            "note": self.note,
        }


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    symbol: str
    side: str
    state: OpportunityState
    created_at: str
    updated_at: str
    zone_low: Decimal
    zone_high: Decimal
    entry_preference: str
    trigger_conditions: tuple[Condition, ...]
    invalidation_conditions: tuple[Condition, ...]
    expires_at: str
    thesis: str
    confidence: float
    risk_request: Mapping[str, Any]
    source: Mapping[str, Any]
    provenance: Mapping[str, Any]
    data_quality: Mapping[str, Any]
    state_history: tuple[StateHistoryEntry, ...]

    def is_expired(self, at: str) -> bool:
        return _parse(at) >= _parse(self.expires_at)

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "opportunity_id": self.opportunity_id,
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "zone": {"low": str(self.zone_low), "high": str(self.zone_high)},
            "entry_preference": self.entry_preference,
            "trigger_conditions": [
                condition.to_contract() for condition in self.trigger_conditions
            ],
            "invalidation_conditions": [
                condition.to_contract() for condition in self.invalidation_conditions
            ],
            "expires_at": self.expires_at,
            "thesis": self.thesis,
            "confidence": self.confidence,
            "risk_request": dict(self.risk_request),
            "source": dict(self.source),
            "provenance": dict(self.provenance),
            "data_quality": dict(self.data_quality),
            "state_history": [entry.to_contract() for entry in self.state_history],
        }


def _parse(value: str) -> datetime:
    """Parse an ISO timestamp, treating an offset-less value as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def validate_condition_drafts(drafts: Iterable[ConditionDraft]) -> tuple[ConditionDraft, ...]:
    """Validate every draft against the closed condition registry."""
    validated: list[ConditionDraft] = []
    for draft in drafts:
        try:
            canonical = validate_draft(draft.type, draft.params)
        except ConditionParamsError as exc:
            raise ConditionDraftError(exc.message, field=exc.field) from exc
        validated.append(replace(draft, type=canonical.type.value, params=dict(canonical.params)))
    return tuple(validated)


def _creation_state(
    draft: ConditionDraft,
    *,
    available_data: Collection[str] | None,
) -> tuple[tuple[str, ...], bool, str | None]:
    spec = condition_spec(draft.type)
    required_data = contract_required_data(spec.type)
    if available_data is None:
        return required_data, True, None
    evaluable = creation_evaluable(spec.type, available_data)
    reason = None
    if not evaluable:
        missing = sorted(spec.required_data - frozenset(available_data))
        reason = "missing required data: " + ", ".join(missing)
    return required_data, evaluable, reason


def assign_conditions(
    drafts: Iterable[ConditionDraft],
    *,
    prefix: str = "C",
    start: int = 1,
    available_data: Collection[str] | None = None,
) -> tuple[Condition, ...]:
    """Assign code-owned IDs after validating the drafts."""
    validated = validate_condition_drafts(drafts)
    conditions: list[Condition] = []
    for index, draft in enumerate(validated, start=start):
        required_data, evaluable, unavailable_reason = _creation_state(
            draft, available_data=available_data
        )
        conditions.append(
            Condition(
                condition_id=f"{prefix}-{index}",
                draft_id=draft.draft_id,
                type=draft.type,
                params=dict(draft.params),
                window_start=normalize_timestamp(str(draft.window.get("start", ""))),
                window_end=normalize_timestamp(str(draft.window.get("end", ""))),
                required_data=required_data,
                evaluable=evaluable,
                unavailable_reason=unavailable_reason,
                description=draft.description,
            )
        )
    return tuple(conditions)


def new_opportunity(
    *,
    opportunity_id: str,
    symbol: str,
    side: str,
    zone_low: Decimal,
    zone_high: Decimal,
    entry_preference: str,
    trigger_conditions: tuple[Condition, ...],
    invalidation_conditions: tuple[Condition, ...],
    expires_at: str,
    thesis: str,
    confidence: float,
    risk_request: Mapping[str, Any],
    source: Mapping[str, Any],
    provenance: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    at: str,
) -> Opportunity:
    if zone_low >= zone_high:
        raise ConditionDraftError("zone_low must be strictly below zone_high", field="zone")
    if not 0.0 <= confidence <= 1.0:
        raise ConditionDraftError("confidence must be within [0, 1]", field="confidence")
    return Opportunity(
        opportunity_id=opportunity_id,
        symbol=symbol,
        side=side,
        state=OpportunityState.OPEN,
        created_at=at,
        updated_at=at,
        zone_low=zone_low,
        zone_high=zone_high,
        entry_preference=entry_preference,
        trigger_conditions=trigger_conditions,
        invalidation_conditions=invalidation_conditions,
        expires_at=expires_at,
        thesis=thesis,
        confidence=confidence,
        risk_request=dict(risk_request),
        source=dict(source),
        provenance=dict(provenance),
        data_quality=dict(data_quality),
        state_history=(
            StateHistoryEntry(
                from_state=None,
                to_state=OpportunityState.OPEN,
                at=at,
                reason_code="CREATED",
                note="opportunity created from a validated LLM draft",
            ),
        ),
    )


def transition(
    opportunity: Opportunity,
    to_state: OpportunityState,
    *,
    at: str,
    reason_code: str,
    note: str,
) -> Opportunity:
    """Move ``OPEN`` to a terminal state, or ``TRIGGERED`` to ``INVALIDATED``.

    History is append-only; every other transition is rejected.
    """
    allowed = _ALLOWED_TRANSITIONS.get(opportunity.state, frozenset())
    if to_state not in allowed:
        raise OpportunityTransitionError(
            f"cannot transition from {opportunity.state.value} to {to_state.value}"
        )
    entry = StateHistoryEntry(
        from_state=opportunity.state,
        to_state=to_state,
        at=at,
        reason_code=reason_code,
        note=note,
    )
    return replace(
        opportunity,
        state=to_state,
        updated_at=at,
        state_history=(*opportunity.state_history, entry),
    )
