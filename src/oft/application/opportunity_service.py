"""Opportunity ledger service: create/list/show/expire/invalidate.

The LLM supplies drafts; this service validates them, assigns code-owned
condition IDs, and persists the contract envelope plus append-only history.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from oft.domain.conditions import (
    SESSION_STATES,
    ConditionEvaluation,
    ConditionParamsError,
    EvaluationContext,
    aggregate_data_quality,
    available_data,
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
# UNKNOWN is a contract enum value, not a measured regime: a recorder that
# cannot tell the session must leave SESSION absent rather than evaluable.
_MEASURED_SESSION_STATES = SESSION_STATES - {"UNKNOWN"}


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
            _condition_records(opportunity),
        )
        return opportunity

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


# -- recorded snapshot -> evaluation context --------------------------------


def _optional_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _bar_series(bars: object, key: str) -> tuple[Decimal, ...] | None:
    if not isinstance(bars, list):
        return None
    values: list[Decimal] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            return None
        value = _optional_decimal(bar.get(key))
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _bar_timestamps(bars: object) -> tuple[str, ...] | None:
    if not isinstance(bars, list) or not bars:
        return None
    values: list[str] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            return None
        value = bar.get("bar_start")
        if not isinstance(value, str) or not value:
            return None
        values.append(value)
    return tuple(values)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _footprint_imbalance(footprint: Mapping[str, Any]) -> Decimal | None:
    bars = footprint.get("bars")
    if not isinstance(bars, list) or not bars:
        return None
    latest = bars[-1]
    cells = _mapping(latest).get("cells")
    if not isinstance(cells, list):
        return None
    strongest: Decimal | None = None
    for cell in cells:
        cell_map = _mapping(cell)
        bid = _optional_decimal(cell_map.get("bid_volume"))
        ask = _optional_decimal(cell_map.get("ask_volume"))
        if bid is None or ask is None:
            continue
        total = bid + ask
        if total <= 0:
            continue
        normalized = abs(bid - ask) / total
        if strongest is None or normalized > strongest:
            strongest = normalized
    return strongest


def _open_interest_delta_pct(section: Mapping[str, Any]) -> Decimal | None:
    points = section.get("points")
    if not isinstance(points, list) or len(points) < 2:
        return None
    first = _optional_decimal(_mapping(points[0]).get("open_interest"))
    last = _optional_decimal(_mapping(points[-1]).get("open_interest"))
    if first is None or last is None or first == 0:
        return None
    return (last - first) / first * Decimal(100)


def _liquidation_notional(
    section: Mapping[str, Any], *, window_start: str | None = None
) -> Decimal | None:
    coverage = section.get("coverage")
    if coverage in (None, "NONE"):
        return None
    long_notional = _optional_decimal(section.get("total_long_notional"))
    short_notional = _optional_decimal(section.get("total_short_notional"))
    if long_notional is None or short_notional is None:
        return None
    if coverage == "SINCE_DEPLOYMENT":
        # Since-deployment coverage only measures the window it actually
        # covers. A recorder that started after the window (or has no end
        # bound yet) cannot report a real zero.
        recorded_start = _parse_timestamp(section.get("coverage_start"))
        recorded_end = _parse_timestamp(section.get("coverage_end"))
        if recorded_start is None or recorded_end is None:
            return None
        window = _parse_timestamp(window_start)
        if window is not None and recorded_start > window:
            return None
    return long_notional + short_notional


def _session_state(snapshot: Mapping[str, Any]) -> str | None:
    session = _mapping(snapshot.get("session"))
    if not session:
        return None
    if bool(session.get("is_no_trading")):
        return "NO_TRADING"
    raw = session.get("underlying_session")
    note = session.get("note")
    if isinstance(note, str) and "not measured" in note.lower():
        # The placeholder written by the metrics-only snapshot builder is an
        # unmeasured field, never a recorded trading regime.
        return None
    # Only a known measured regime counts. Every other label (UNKNOWN,
    # "unknown", empty, garbage) is absent: coercing it to the measured value
    # "UNKNOWN" would let unmeasured data satisfy a SESSION_STATE condition.
    if not isinstance(raw, str) or raw not in _MEASURED_SESSION_STATES:
        return None
    return raw


def evaluation_context_from_snapshot(
    snapshot: Mapping[str, Any], *, as_of: str
) -> EvaluationContext:
    """Build the exact-decimal display context from a recorded report snapshot."""
    contract = _mapping(snapshot.get("contract_specs"))
    tick_size = _optional_decimal(contract.get("tick_size"))
    if tick_size is not None and tick_size <= 0:
        tick_size = None
    bars = snapshot.get("bars")
    # An empty or malformed series is absent, never an empty tuple: ``volume``
    # must stay None (missing) instead of collapsing to zero.
    closes = _bar_series(bars, "close") or None
    cvds = _bar_series(bars, "cvd") or None
    volumes = _bar_series(bars, "volume") or None
    deltas = _bar_series(bars, "delta") or None
    ois = _bar_series(bars, "open_interest") or None
    bar_timestamps = _bar_timestamps(bars)
    last_price = _optional_decimal(snapshot.get("last_price"))
    if last_price is None:
        last_price = _optional_decimal(snapshot.get("mark_price"))
    if last_price is None and closes:
        last_price = closes[-1]
    previous_price = closes[-2] if closes is not None and len(closes) >= 2 else None
    orderflow = _mapping(snapshot.get("orderflow"))
    cvd_section = _mapping(orderflow.get("cvd"))
    summary = _mapping(cvd_section.get("summary"))
    delta = _optional_decimal(summary.get("delta"))
    points = cvd_section.get("points")
    if cvds is None and isinstance(points, list):
        # A hole anywhere in the series makes it unavailable; never compress.
        collected: list[Decimal] = []
        complete = True
        for point in points:
            value = _optional_decimal(_mapping(point).get("cvd"))
            if value is None:
                complete = False
                break
            collected.append(value)
        cvds = tuple(collected) if complete and collected else None
    cvd = cvds[-1] if cvds else None
    volume = sum(volumes, Decimal(0)) if volumes is not None else None
    funding_section = _mapping(orderflow.get("funding"))
    history = funding_section.get("history")
    funding_rate = (
        _optional_decimal(funding_section.get("current_rate"))
        if isinstance(history, list) and history
        else None
    )
    window = _mapping(snapshot.get("window"))
    window_start = window.get("start")
    return EvaluationContext(
        as_of=as_of,
        last_price=last_price,
        previous_price=previous_price,
        tick_size=tick_size,
        delta=delta,
        cvd=cvd,
        cvd_series=cvds,
        price_series=closes,
        oi_delta_pct=_open_interest_delta_pct(_mapping(orderflow.get("open_interest"))),
        funding_rate=funding_rate,
        liquidation_notional=_liquidation_notional(
            _mapping(orderflow.get("liquidations")),
            window_start=window_start if isinstance(window_start, str) else None,
        ),
        volume=volume,
        footprint_imbalance=_footprint_imbalance(_mapping(orderflow.get("footprint"))),
        session_state=_session_state(snapshot),
        bar_timestamps=bar_timestamps,
        delta_series=deltas,
        volume_series=volumes,
        oi_series=ois,
    )


def available_data_from_snapshot(snapshot: Mapping[str, Any]) -> frozenset[str]:
    context = evaluation_context_from_snapshot(snapshot, as_of="1970-01-01T00:00:00.000Z")
    return available_data(context)


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
    "OpportunityContractError",
    "OpportunityNotFound",
    "OpportunityService",
    "opportunity_payload",
    "opportunity_summary",
]
