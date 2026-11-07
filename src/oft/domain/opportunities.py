"""Structured opportunity ledger and the code-only state machine (ADR-0006).

Only the deterministic trigger evaluator (code) may move an opportunity out of
``OPEN``. The LLM supplies drafts; code assigns condition IDs, validates the
required parameters, and appends immutable history entries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

SUPPORTED_CONDITION_TYPES = frozenset({"PRICE_TOUCH_ZONE"})
OPEN = "OPEN"


class OpportunityState(StrEnum):
    OPEN = "OPEN"
    TRIGGERED = "TRIGGERED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


_TERMINAL_STATES = frozenset(
    {OpportunityState.TRIGGERED, OpportunityState.EXPIRED, OpportunityState.INVALIDATED}
)


class OpportunityTransitionError(Exception):
    """The requested state change is not allowed by the state machine."""

    code = "POSITION_STATE_INVALID"


class ConditionDraftError(Exception):
    """A draft condition is unsupported or missing required parameters."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


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
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ConditionDraftError(f"{field} is required for PRICE_TOUCH_ZONE", field=field)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ConditionDraftError(f"{field} must be an exact decimal", field=field) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ConditionDraftError(f"{field} must be a positive decimal", field=field)
    return parsed


def validate_condition_drafts(drafts: Iterable[ConditionDraft]) -> tuple[ConditionDraft, ...]:
    """Validate every draft against the closed condition registry."""
    validated: list[ConditionDraft] = []
    for draft in drafts:
        if draft.type not in SUPPORTED_CONDITION_TYPES:
            raise ConditionDraftError(
                f"condition type {draft.type} is not supported in this slice",
                field="type",
            )
        low = _decimal(draft.params.get("price_low"), "price_low")
        high = _decimal(draft.params.get("price_high"), "price_high")
        if low >= high:
            raise ConditionDraftError(
                "price_low must be strictly below price_high",
                field="price_low",
            )
        validated.append(draft)
    return tuple(validated)


def assign_conditions(
    drafts: Iterable[ConditionDraft],
    *,
    prefix: str = "C",
    start: int = 1,
) -> tuple[Condition, ...]:
    """Assign code-owned IDs after validating the drafts."""
    validated = validate_condition_drafts(drafts)
    conditions: list[Condition] = []
    for index, draft in enumerate(validated, start=start):
        conditions.append(
            Condition(
                condition_id=f"{prefix}-{index}",
                draft_id=draft.draft_id,
                type=draft.type,
                params=dict(draft.params),
                window_start=str(draft.window.get("start", "")),
                window_end=str(draft.window.get("end", "")),
                required_data=tuple(draft.required_data),
                evaluable=True,
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
    """Move ``OPEN`` to exactly one terminal state; history is append-only."""
    if opportunity.state is not OpportunityState.OPEN:
        raise OpportunityTransitionError(
            f"cannot transition from terminal state {opportunity.state.value}"
        )
    if to_state is OpportunityState.OPEN:
        raise OpportunityTransitionError("transition target must differ from OPEN")
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
