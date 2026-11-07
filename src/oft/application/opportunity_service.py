"""Opportunity ledger service: create/list/show/expire/invalidate.

The LLM supplies drafts; this service validates them, assigns code-owned
condition IDs, and persists the contract envelope plus append-only history.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from oft.domain.opportunities import (
    Condition,
    ConditionDraft,
    ConditionDraftError,
    Opportunity,
    OpportunityState,
    OpportunityTransitionError,
    assign_conditions,
    new_opportunity,
    transition,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    OpportunityEventRecord,
    OpportunityRecord,
    StorePort,
)

SUPPORTED_INVALIDATION_TYPES = frozenset(
    {
        "PRICE_CROSS_ABOVE",
        "PRICE_CROSS_BELOW",
        "PRICE_OUTSIDE_ZONE",
        "DELTA_ABOVE",
        "DELTA_BELOW",
        "OI_DELTA_ABOVE",
        "OI_DELTA_BELOW",
        "FUNDING_ABOVE",
        "FUNDING_BELOW",
        "TIME_EXPIRED",
        "MANUAL",
    }
)


class OpportunityNotFound(Exception):
    code = "NOT_FOUND"


class OpportunityService:
    def __init__(self, *, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    # -- reads -----------------------------------------------------------

    def list(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[Opportunity, ...]:
        normalized = state.upper() if state else None
        return tuple(
            _to_domain(record)
            for record in self._store.list_opportunities(
                state=normalized, limit=limit, offset=offset
            )
        )

    def count(self, *, state: str | None = None) -> int:
        normalized = state.upper() if state else None
        return self._store.count_opportunities(state=normalized)

    def show(self, opportunity_id: str) -> Opportunity:
        record = self._store.get_opportunity(opportunity_id)
        if record is None:
            raise OpportunityNotFound(f"Opportunity {opportunity_id} does not exist.")
        return _to_domain(record)

    def timeline(self, opportunity_id: str) -> tuple[dict[str, Any], ...]:
        opportunity = self.show(opportunity_id)
        events = self._store.list_opportunity_events(opportunity_id)
        if events:
            return tuple(
                {
                    "from_state": event.from_state,
                    "to_state": event.to_state,
                    "at": event.at,
                    "reason_code": event.reason_code,
                    "note": event.note,
                }
                for event in events
            )
        return tuple(entry.to_contract() for entry in opportunity.state_history)

    # -- writes ----------------------------------------------------------

    def create_from_decision(
        self,
        *,
        opportunity_draft: Any,
        subject: dict[str, Any],
        decision_id: str,
        scan_id: str,
        report_id: str,
        confidence: float,
        provenance: dict[str, Any],
        data_quality: dict[str, Any],
        at: str,
    ) -> Opportunity:
        trigger_drafts = tuple(
            ConditionDraft(
                draft_id=draft.draft_id,
                type=draft.type,
                params=draft.params.model_dump(),
                window={"start": draft.window.start, "end": draft.window.end},
                required_data=tuple(draft.required_data),
                description=draft.description,
            )
            for draft in opportunity_draft.trigger_conditions
        )
        trigger_conditions = assign_conditions(trigger_drafts)
        invalidation_conditions = tuple(
            _invalidation_condition(
                index,
                draft,
                at=at,
                expires_at=opportunity_draft.expires_at,
            )
            for index, draft in enumerate(opportunity_draft.invalidation_conditions, start=1)
        )
        opportunity = new_opportunity(
            opportunity_id=f"OPP-{_digest(decision_id)}",
            symbol=str(subject["symbol"]),
            side=opportunity_draft.side,
            zone_low=Decimal(opportunity_draft.zone_low),
            zone_high=Decimal(opportunity_draft.zone_high),
            entry_preference=opportunity_draft.entry_preference,
            trigger_conditions=trigger_conditions,
            invalidation_conditions=invalidation_conditions,
            expires_at=opportunity_draft.expires_at,
            thesis=opportunity_draft.thesis,
            confidence=confidence,
            risk_request=_risk_request(opportunity_draft.risk_request),
            source={"scan_id": scan_id, "report_id": report_id, "decision_id": decision_id},
            provenance=provenance,
            data_quality=data_quality,
            at=at,
        )
        created = opportunity.state_history[0]
        self._store.save_opportunity(
            _record(opportunity),
            OpportunityEventRecord(
                opportunity_id=opportunity.opportunity_id,
                seq=1,
                at=created.at,
                from_state=None,
                to_state=created.to_state.value,
                reason_code=created.reason_code,
                note=created.note,
            ),
        )
        return opportunity

    def expire(self, opportunity_id: str, *, reason: str = "past expires_at") -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.EXPIRED,
            reason_code="EXPIRED",
            note=reason,
        )

    def invalidate(self, opportunity_id: str, *, reason: str) -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.INVALIDATED,
            reason_code="REJECTED",
            note=reason,
        )

    def mark_triggered(self, opportunity_id: str, *, trigger_id: str) -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.TRIGGERED,
            reason_code="TRIGGER_MET",
            note=trigger_id,
        )

    def _transition(
        self,
        opportunity_id: str,
        target: OpportunityState,
        *,
        reason_code: str,
        note: str,
    ) -> Opportunity:
        opportunity = self.show(opportunity_id)
        at = _timestamp(self._clock)
        updated = transition(
            opportunity,
            target,
            at=at,
            reason_code=reason_code,
            note=note,
        )
        record = _record(updated)
        event = OpportunityEventRecord(
            opportunity_id=opportunity_id,
            seq=len(updated.state_history),
            at=at,
            from_state=opportunity.state.value,
            to_state=target.value,
            reason_code=reason_code,
            note=note,
        )
        if not self._store.transition_opportunity(record, event):
            raise OpportunityTransitionError("the opportunity is no longer OPEN")
        return updated


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()


def _timestamp(clock: ClockPort) -> str:
    from oft.domain.time import utc_timestamp

    return utc_timestamp(clock.now())


def _risk_request(draft: Any) -> dict[str, Any]:
    return {
        "entry_price": draft.entry_price,
        "stop_price": draft.stop_price,
        "quantity": None,
        "risk_pct": draft.risk_pct,
        "leverage": draft.leverage,
        "notional_usd": draft.max_notional_usd,
    }


_CONTRACT_CONDITION_TYPES = frozenset(
    {
        "PRICE_CROSS_ABOVE",
        "PRICE_CROSS_BELOW",
        "PRICE_INSIDE_ZONE",
        "PRICE_TOUCH_ZONE",
        "PRICE_OUTSIDE_ZONE",
        "DELTA_ABOVE",
        "DELTA_BELOW",
        "CVD_ABOVE",
        "CVD_BELOW",
        "CVD_DIVERGENCE",
        "OI_DELTA_ABOVE",
        "OI_DELTA_BELOW",
        "FUNDING_ABOVE",
        "FUNDING_BELOW",
        "LIQUIDATION_NOTIONAL_ABOVE",
        "VOLUME_ABOVE",
        "FOOTPRINT_IMBALANCE_ABOVE",
        "SESSION_STATE",
        "TIME_WINDOW",
    }
)


def _invalidation_condition(
    index: int,
    draft: Any,
    *,
    at: str,
    expires_at: str,
) -> Condition:
    supported = draft.type in SUPPORTED_INVALIDATION_TYPES
    contract_type = draft.type if draft.type in _CONTRACT_CONDITION_TYPES else "TIME_WINDOW"
    params: dict[str, Any] = {
        "price": draft.price,
        "threshold": draft.threshold,
        "window_end": draft.window_end,
        "original_type": draft.type,
    }
    return Condition(
        condition_id=f"I-{index}",
        draft_id=f"i{index}",
        type=contract_type,
        params=params,
        window_start=at,
        window_end=draft.window_end or expires_at,
        required_data=("MARK_PRICE",),
        evaluable=False,
        unavailable_reason=(None if supported else "condition type is not evaluated in this slice"),
        description=draft.description,
    )


def _record(opportunity: Opportunity) -> OpportunityRecord:
    return OpportunityRecord(
        opportunity_id=opportunity.opportunity_id,
        symbol=opportunity.symbol,
        state=opportunity.state.value,
        created_at=opportunity.created_at,
        updated_at=opportunity.updated_at,
        payload_json=json.dumps(opportunity.to_contract(), sort_keys=True),
    )


def _to_domain(record: OpportunityRecord) -> Opportunity:
    payload = json.loads(record.payload_json)
    return _opportunity_from_contract(payload)


def _opportunity_from_contract(payload: dict[str, Any]) -> Opportunity:
    from oft.domain.opportunities import StateHistoryEntry

    conditions = tuple(_condition_from_contract(item) for item in payload["trigger_conditions"])
    invalidation = tuple(
        _condition_from_contract(item) for item in payload.get("invalidation_conditions", [])
    )
    history = tuple(
        StateHistoryEntry(
            from_state=(
                None if item["from_state"] is None else OpportunityState(item["from_state"])
            ),
            to_state=OpportunityState(item["to_state"]),
            at=item["at"],
            reason_code=item["reason_code"],
            note=item["note"],
        )
        for item in payload["state_history"]
    )
    return Opportunity(
        opportunity_id=payload["opportunity_id"],
        symbol=payload["symbol"],
        side=payload["side"],
        state=OpportunityState(payload["state"]),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        zone_low=Decimal(payload["zone"]["low"]),
        zone_high=Decimal(payload["zone"]["high"]),
        entry_preference=payload["entry_preference"],
        trigger_conditions=conditions,
        invalidation_conditions=invalidation,
        expires_at=payload["expires_at"],
        thesis=payload["thesis"],
        confidence=payload["confidence"],
        risk_request=payload["risk_request"],
        source=payload["source"],
        provenance=payload["provenance"],
        data_quality=payload["data_quality"],
        state_history=history,
    )


def _condition_from_contract(payload: dict[str, Any]) -> Condition:
    return Condition(
        condition_id=payload["condition_id"],
        draft_id=payload.get("draft_id", ""),
        type=payload["type"],
        params=payload["params"],
        window_start=payload["window"]["start"],
        window_end=payload["window"]["end"],
        required_data=tuple(payload["required_data"]),
        evaluable=payload["evaluable"],
        unavailable_reason=payload.get("unavailable_reason"),
        last_value=payload.get("last_value"),
        last_evaluated_at=payload.get("last_evaluated_at"),
        last_result=payload.get("last_result"),
    )


def opportunity_payload(opportunity: Opportunity) -> dict[str, Any]:
    return opportunity.to_contract()


def opportunity_summary(opportunity: Opportunity) -> dict[str, Any]:
    return {
        "opportunity_id": opportunity.opportunity_id,
        "symbol": opportunity.symbol,
        "side": opportunity.side,
        "state": opportunity.state.value,
        "confidence": opportunity.confidence,
        "expires_at": opportunity.expires_at,
        "created_at": opportunity.created_at,
        "updated_at": opportunity.updated_at,
        "source": dict(opportunity.source),
    }


__all__ = [
    "ConditionDraftError",
    "OpportunityNotFound",
    "OpportunityService",
    "opportunity_payload",
    "opportunity_summary",
]
