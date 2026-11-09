"""Opportunity ledger service: create/list/show/expire/invalidate.

The LLM supplies drafts; this service validates them, assigns code-owned
condition IDs, and persists the contract envelope plus append-only history.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from oft.application.condition_context import (
    available_data_from_snapshot as available_data_from_snapshot,
)
from oft.application.condition_context import (
    bar_series as _bar_series,
)
from oft.application.condition_context import (
    bar_timestamps as _bar_timestamps,
)
from oft.application.condition_context import (
    evaluation_context_from_snapshot as evaluation_context_from_snapshot,
)
from oft.application.condition_context import (
    footprint_imbalance as _footprint_imbalance,
)
from oft.application.condition_context import (
    liquidation_notional as _liquidation_notional,
)
from oft.application.condition_context import (
    open_interest_delta_pct as _open_interest_delta_pct,
)
from oft.application.condition_context import (
    optional_decimal as _optional_decimal,
)
from oft.application.condition_context import (
    parse_timestamp as _parse_timestamp,
)
from oft.application.condition_context import (
    session_state as _session_state,
)
from oft.domain.conditions import (
    ConditionEvaluation,
    ConditionParamsError,
    aggregate_data_quality,
    evaluate_condition,
    map_invalidation_draft,
)
from oft.domain.opportunities import (
    Condition,
    ConditionDraft,
    ConditionDraftError,
    Opportunity,
    OpportunityContractError,
    OpportunityState,
    OpportunityTransitionError,
    assign_conditions,
    new_opportunity,
    transition,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    ConditionRecord,
    OpportunityEventRecord,
    OpportunityRecord,
    StorePort,
)

_LOGGER = structlog.get_logger(__name__)


class OpportunityNotFound(Exception):
    code = "NOT_FOUND"


class OpportunityConflictError(Exception):
    """A replay of an existing opportunity id carries different content."""

    code = "CONFLICT"


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
        available_data: Collection[str] | None = None,
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
        trigger_conditions = assign_conditions(trigger_drafts, available_data=available_data)
        invalidation_conditions = tuple(
            _invalidation_condition(
                index,
                draft,
                at=at,
                expires_at=opportunity_draft.expires_at,
                zone_low=opportunity_draft.zone_low,
                zone_high=opportunity_draft.zone_high,
                available_data=available_data,
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
        persisted = self._store.save_opportunity(
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
            _condition_records(opportunity),
        )
        if persisted:
            return opportunity
        # A duplicate create is an atomic no-op in the store. It is only an
        # idempotent replay when the stored payload is byte-identical to what
        # this call would have written; otherwise the create did NOT persist
        # and must never be reported as a success.
        stored_record = self._store.get_opportunity(opportunity.opportunity_id)
        if stored_record is None:
            raise OpportunityConflictError(
                f"opportunity {opportunity.opportunity_id} already exists "
                "but its stored payload could not be loaded"
            )
        stored = _to_domain(stored_record)
        submitted = json.dumps(opportunity.to_contract(), sort_keys=True)
        persisted_contract = json.dumps(stored.to_contract(), sort_keys=True)
        if submitted == persisted_contract:
            _LOGGER.info(
                "opportunity_create_idempotent_replay",
                opportunity_id=opportunity.opportunity_id,
                decision_id=decision_id,
            )
            return stored
        _LOGGER.warning(
            "opportunity_create_conflict",
            opportunity_id=opportunity.opportunity_id,
            decision_id=decision_id,
            reason="stored payload differs from the replayed decision",
        )
        raise OpportunityConflictError(
            f"opportunity {opportunity.opportunity_id} already exists with different content"
        )

    def expire_due(self, now: str | None = None) -> tuple[Opportunity, ...]:
        """Transition every OPEN opportunity whose ``expires_at`` has passed.

        Due rows are selected in SQL. Each row is independent: a row that lost
        a concurrent transition race is logged and skipped, never aborting the
        batch.
        """
        at = now or _timestamp(self._clock)
        expired: list[Opportunity] = []
        rows = self._store.list_due_opportunities(at)
        for record in rows:
            try:
                expired.append(
                    self.expire(
                        record.opportunity_id,
                        reason="expires_at reached",
                        at=at,
                    )
                )
            except (
                OpportunityTransitionError,
                OpportunityNotFound,
                OpportunityContractError,
            ) as exc:
                _LOGGER.warning(
                    "opportunity_expire_due_skipped",
                    opportunity_id=record.opportunity_id,
                    error=str(exc),
                )
        return tuple(expired)

    def expire(
        self,
        opportunity_id: str,
        *,
        reason: str = "past expires_at",
        at: str | None = None,
    ) -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.EXPIRED,
            reason_code="EXPIRED",
            note=reason,
            at=at,
        )

    def invalidate(self, opportunity_id: str, *, reason: str, at: str | None = None) -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.INVALIDATED,
            reason_code="REJECTED",
            note=reason,
            at=at,
        )

    def mark_triggered(
        self, opportunity_id: str, *, trigger_id: str, at: str | None = None
    ) -> Opportunity:
        return self._transition(
            opportunity_id,
            OpportunityState.TRIGGERED,
            reason_code="TRIGGER_MET",
            note=trigger_id,
            at=at,
        )

    def _transition(
        self,
        opportunity_id: str,
        target: OpportunityState,
        *,
        reason_code: str,
        note: str,
        at: str | None = None,
    ) -> Opportunity:
        opportunity = self.show(opportunity_id)
        resolved_at = at or _timestamp(self._clock)
        updated = transition(
            opportunity,
            target,
            at=resolved_at,
            reason_code=reason_code,
            note=note,
        )
        record = _record(updated)
        event = OpportunityEventRecord(
            opportunity_id=opportunity_id,
            seq=len(updated.state_history),
            at=resolved_at,
            from_state=opportunity.state.value,
            to_state=target.value,
            reason_code=reason_code,
            note=note,
        )
        if not self._store.transition_opportunity(record, event):
            raise OpportunityTransitionError("the opportunity is no longer OPEN")
        return updated

    # -- display-only evaluation (side-effect free) ----------------------

    def evaluate_display(self, opportunity_id: str, *, at: str | None = None) -> dict[str, Any]:
        """Evaluate every condition against recorded data; never mutates state."""
        opportunity = self.show(opportunity_id)
        now = at or _timestamp(self._clock)
        all_conditions = (
            *opportunity.trigger_conditions,
            *opportunity.invalidation_conditions,
        )
        if opportunity.state is OpportunityState.EXPIRED or opportunity.is_expired(now):
            evaluations = tuple(_expired_evaluation(condition, now) for condition in all_conditions)
            return _display_payload(opportunity, all_conditions, evaluations, now, False)
        snapshot = self._report_snapshot(opportunity)
        if snapshot is None:
            # No market data: every condition is unavailable. Never let an
            # as_of-only context produce a success story from a pruned report.
            evaluations = tuple(
                _unavailable_evaluation(condition, now, "report unavailable")
                for condition in all_conditions
            )
            return _display_payload(opportunity, all_conditions, evaluations, now, False)
        context = evaluation_context_from_snapshot(snapshot, as_of=now)
        evaluations = tuple(evaluate_condition(condition, context) for condition in all_conditions)
        trigger_count = len(opportunity.trigger_conditions)
        trigger_evaluations = evaluations[:trigger_count]
        would_trigger = (
            opportunity.state is OpportunityState.OPEN
            and bool(trigger_evaluations)
            and all(item.met and item.evaluable for item in trigger_evaluations)
        )
        return _display_payload(opportunity, all_conditions, evaluations, now, would_trigger)

    def _report_snapshot(self, opportunity: Opportunity) -> dict[str, Any] | None:
        report_id = str(opportunity.source.get("report_id", ""))
        if not report_id:
            return None
        report = self._store.get_report(report_id)
        if report is None:
            return None
        try:
            snapshot = json.loads(report.snapshot_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            _LOGGER.warning(
                "opportunity_report_snapshot_unavailable",
                opportunity_id=opportunity.opportunity_id,
                report_id=report_id,
            )
            return None
        if not isinstance(snapshot, dict):
            _LOGGER.warning(
                "opportunity_report_snapshot_unavailable",
                opportunity_id=opportunity.opportunity_id,
                report_id=report_id,
            )
            return None
        return snapshot


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


def _invalidation_condition(
    index: int,
    draft: Any,
    *,
    at: str,
    expires_at: str,
    zone_low: str,
    zone_high: str,
    available_data: Collection[str] | None,
) -> Condition:
    try:
        mapped = map_invalidation_draft(
            draft.type,
            price=draft.price,
            threshold=draft.threshold,
            window_end=draft.window_end,
            created_at=at,
            expires_at=expires_at,
            zone_low=zone_low,
            zone_high=zone_high,
            available_data=(None if available_data is None else frozenset(available_data)),
        )
    except ConditionParamsError as exc:
        raise ConditionDraftError(exc.message, field=exc.field) from exc
    return Condition(
        condition_id=f"I-{index}",
        draft_id=f"i{index}",
        type=mapped.type.value,
        params=dict(mapped.params),
        window_start=mapped.window_start,
        window_end=mapped.window_end,
        required_data=mapped.required_data,
        evaluable=mapped.evaluable,
        unavailable_reason=mapped.unavailable_reason,
        description=draft.description,
    )


def _condition_records(opportunity: Opportunity) -> tuple[ConditionRecord, ...]:
    records: list[ConditionRecord] = []
    for kind, conditions in (
        ("TRIGGER", opportunity.trigger_conditions),
        ("INVALIDATION", opportunity.invalidation_conditions),
    ):
        for seq, condition in enumerate(conditions, start=1):
            records.append(
                ConditionRecord(
                    condition_id=condition.condition_id,
                    opportunity_id=opportunity.opportunity_id,
                    kind=kind,
                    seq=seq,
                    type=condition.type,
                    params_json=json.dumps(dict(condition.params), sort_keys=True),
                    window_start=condition.window_start,
                    window_end=condition.window_end,
                    required_data_json=json.dumps(list(condition.required_data), sort_keys=True),
                    evaluable=condition.evaluable,
                    unavailable_reason=condition.unavailable_reason,
                    last_value=condition.last_value,
                    last_evaluated_at=condition.last_evaluated_at,
                    last_result=condition.last_result,
                )
            )
    return tuple(records)


def _expired_evaluation(condition: Condition, at: str) -> ConditionEvaluation:
    return ConditionEvaluation(
        condition_id=condition.condition_id,
        type=condition.type,
        met=False,
        observed_value=None,
        threshold=None,
        evaluable=condition.evaluable,
        unavailable_reason=condition.unavailable_reason,
        data_quality="MISSING",
        note="opportunity is expired",
        evaluated_at=at,
    )


def _unavailable_evaluation(condition: Condition, at: str, reason: str) -> ConditionEvaluation:
    return ConditionEvaluation(
        condition_id=condition.condition_id,
        type=condition.type,
        met=False,
        observed_value=None,
        threshold=None,
        evaluable=False,
        unavailable_reason=reason,
        data_quality="MISSING",
        note=reason,
        evaluated_at=at,
    )


def _display_payload(
    opportunity: Opportunity,
    conditions: Sequence[Condition],
    evaluations: Sequence[ConditionEvaluation],
    as_of: str,
    would_trigger: bool,
) -> dict[str, Any]:
    return {
        "opportunity_id": opportunity.opportunity_id,
        "state": opportunity.state.value,
        "conditions": [condition.to_contract() for condition in conditions],
        "evaluations": [evaluation.to_contract() for evaluation in evaluations],
        "would_trigger": would_trigger,
        "as_of": as_of,
        "data_quality": aggregate_data_quality(
            evaluation.data_quality for evaluation in evaluations
        ),
    }


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
    """Reconstruct the domain object, failing with a typed data error.

    Persisted JSON is untrusted: a truncated or hand-edited row must never
    surface as a raw ``KeyError``/``ValueError``/``InvalidOperation`` out of a
    read path.
    """
    try:
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise TypeError("the persisted payload is not an object")
        return _opportunity_from_contract(payload)
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        InvalidOperation,
    ) as exc:
        raise OpportunityContractError(
            f"opportunity {record.opportunity_id} has a corrupt contract payload"
        ) from exc


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
    "OpportunityConflictError",
    "OpportunityContractError",
    "OpportunityNotFound",
    "OpportunityService",
    # Shared recorded-snapshot mapping, re-exported for existing callers.
    "_bar_series",
    "_bar_timestamps",
    "_footprint_imbalance",
    "_liquidation_notional",
    "_open_interest_delta_pct",
    "_optional_decimal",
    "_parse_timestamp",
    "_session_state",
    "available_data_from_snapshot",
    "evaluation_context_from_snapshot",
    "opportunity_payload",
    "opportunity_summary",
]
