"""Deterministic trigger evaluation, snapshot freezing, and batch re-check queue.

Only code produces trigger events. The evaluator uses one frozen snapshot per
symbol per tick, records the condition results and hash, and keeps one trigger
per ``(opportunity, snapshot_hash)`` via a unique idempotency key. A stale or
unavailable snapshot never triggers. No order, fill, position, or LLM call is
made in this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import structlog

from oft.application.kill_switch import record_audit
from oft.application.opportunity_service import (
    OpportunityNotFound,
    OpportunityService,
    _to_domain,
)
from oft.application.trigger_snapshot import (
    ReportSnapshotProvider,
    TriggerSnapshotProvider,
)
from oft.domain.opportunities import (
    Opportunity,
    OpportunityContractError,
    OpportunityState,
    transition,
)
from oft.domain.time import utc_timestamp
from oft.domain.trigger import (
    DEFAULT_STALE_AFTER_SECONDS,
    ConditionResult,
    TriggerEvaluation,
    TriggerSnapshot,
    contract_data_quality,
    evaluate_trigger,
    snapshot_hash,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    OpportunityEventRecord,
    OpportunityRecord,
    StorePort,
    TriggerEventRecord,
)

DEFAULT_BATCH_CAP = 500
_LOGGER = structlog.get_logger(__name__)


class TriggerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TriggerNotFound(TriggerError):
    def __init__(self, trigger_id: str) -> None:
        super().__init__("NOT_FOUND", f"Trigger {trigger_id} does not exist.")


@dataclass(frozen=True)
class TriggerRun:
    trigger: TriggerEventRecord | None
    evaluation: TriggerEvaluation
    opportunity: Opportunity
    created: bool = False


@dataclass(frozen=True)
class TriggerBatchOutcome:
    """Counts from one batch evaluation of every OPEN opportunity."""

    open: int = 0
    evaluated: int = 0
    triggered: int = 0
    skipped: int = 0
    stale: int = 0
    unavailable: int = 0
    trigger_ids: tuple[str, ...] = ()
    open_total: int = 0
    capped: bool = False


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()


def _condition_contract(result: ConditionResult, *, fallback_at: str) -> dict[str, Any]:
    """Map one condition result to the ``trigger-event`` contract item."""
    return {
        "condition_id": result.condition_id,
        "type": result.type,
        "met": result.met,
        "observed_value": result.observed_value,
        "threshold": result.threshold,
        "evaluated_at": result.evaluated_at or fallback_at,
        "data_quality": contract_data_quality(result.data_quality),
        "evaluable": result.evaluable,
        "note": result.note or "",
    }


class TriggerService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: TriggerSnapshotProvider | None = None,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        batch_cap: int = DEFAULT_BATCH_CAP,
    ) -> None:
        self._store = store
        self._clock = clock
        self._opportunities = OpportunityService(store=store, clock=clock)
        self._provider = provider or ReportSnapshotProvider(store=store)
        self._stale_after_seconds = stale_after_seconds
        self._batch_cap = batch_cap

    # -- manual path -----------------------------------------------------

    def evaluate(
        self,
        opportunity_id: str,
        *,
        price: Decimal | None = None,
        as_of: str | None = None,
        snapshot_id: str | None = None,
        data_quality: str = "COMPLETE",
        stale_after_seconds: int | None = None,
    ) -> TriggerRun:
        opportunity = self._opportunities.show(opportunity_id)
        now = utc_timestamp(self._clock.now())
        if opportunity.state is not OpportunityState.OPEN:
            existing = self._existing_for(opportunity_id)
            if existing is not None and opportunity.state is OpportunityState.TRIGGERED:
                return TriggerRun(
                    trigger=existing,
                    evaluation=_not_evaluated(now, "opportunity is already TRIGGERED"),
                    opportunity=opportunity,
                )
            raise TriggerError(
                "CONFLICT", f"opportunity is {opportunity.state.value}; no trigger is possible"
            )
        if opportunity.is_expired(now):
            expired = self._opportunities.expire(opportunity_id, reason="expires_at reached")
            return TriggerRun(
                trigger=None,
                evaluation=_not_evaluated(now, "opportunity expired"),
                opportunity=expired,
            )
        budget = self._stale_after_seconds if stale_after_seconds is None else stale_after_seconds
        if price is None and as_of is None:
            frozen = self._provider.snapshot_for(opportunity)
            if frozen is None:
                return self._unavailable_run(opportunity, now)
            if snapshot_id is not None:
                frozen = replace(frozen, snapshot_id=snapshot_id)
            return self._evaluate_and_record(
                opportunity,
                frozen,
                now=now,
                stale_after_seconds=budget,
            )

        # The provider is only consulted for a field the caller did not
        # supply; supplying both must not emit a spurious unavailable warning.
        report_snapshot = (
            self._provider.snapshot_for(opportunity) if price is None or as_of is None else None
        )
        if (
            report_snapshot is not None
            and contract_data_quality(report_snapshot.data_quality) == "STALE"
        ):
            # Never let a manual override launder a STALE report into a fresh
            # snapshot: the job path blocks it, and so must this one.
            reasons = "; ".join(report_snapshot.quality_reasons) or "data quality is STALE"
            raise TriggerError(
                "VALIDATION_FAILED",
                f"cannot override a STALE source snapshot: {reasons}",
            )
        resolved_as_of = as_of or (report_snapshot.as_of if report_snapshot is not None else None)
        resolved_price = (
            price
            if price is not None
            else (report_snapshot.last_price if report_snapshot is not None else None)
        )
        if resolved_as_of is None or resolved_price is None:
            return self._unavailable_run(opportunity, now)
        resolved_id = snapshot_id or (
            report_snapshot.snapshot_id if report_snapshot is not None else ""
        )
        frozen = TriggerSnapshot(
            symbol=opportunity.symbol,
            as_of=resolved_as_of,
            last_price=resolved_price,
            # Preserve the source report's quality for every field not
            # overridden; only a fully manual snapshot falls back to the
            # caller's label.
            data_quality=(
                report_snapshot.data_quality if report_snapshot is not None else data_quality
            ),
            quality_reasons=(
                report_snapshot.quality_reasons if report_snapshot is not None else ()
            ),
            snapshot_id=resolved_id,
        )
        return self._evaluate_and_record(
            opportunity,
            frozen,
            now=now,
            stale_after_seconds=budget,
        )

    # -- batch/job path --------------------------------------------------

    def evaluate_all(self, now: str | None = None) -> TriggerBatchOutcome:
        """Expire due opportunities, then evaluate every remaining OPEN one.

        Exactly one snapshot per symbol is fetched per tick. The batch is
        idempotent: an existing ``(opportunity, snapshot_hash)`` trigger is
        returned, never duplicated. A concurrent state transition is logged and
        skipped. The database is the queue, so a restart reconstructs it.
        """
        at = now or utc_timestamp(self._clock.now())
        self._opportunities.expire_due(at)
        records = self._store.list_opportunities(state="OPEN", limit=self._batch_cap)
        open_total = self._store.count_opportunities(state="OPEN")
        capped = len(records) >= self._batch_cap
        if capped:
            _LOGGER.warning(
                "trigger_batch_capped",
                loaded=len(records),
                open_total=open_total,
                batch_cap=self._batch_cap,
            )
        # One provider snapshot per ``(symbol, report_id)`` per tick. Two
        # opportunities on one symbol backed by different source reports must
        # never share the wrong snapshot.
        snapshots: dict[tuple[str, str], TriggerSnapshot | None] = {}
        evaluated = triggered = skipped = stale = unavailable = 0
        trigger_ids: list[str] = []
        for record in records:
            try:
                opportunity = _to_domain(record)
            except OpportunityContractError as exc:
                # One corrupt payload must not abort the whole tick: isolate it
                # exactly as ``expire_due`` does and keep evaluating the rest.
                unavailable += 1
                _LOGGER.warning(
                    "trigger_opportunity_skipped",
                    opportunity_id=record.opportunity_id,
                    error=str(exc),
                )
                continue
            cache_key = (opportunity.symbol, str(opportunity.source.get("report_id", "")))
            if cache_key not in snapshots:
                snapshots[cache_key] = self._provider.snapshot_for(opportunity)
            frozen = snapshots[cache_key]
            if frozen is None:
                unavailable += 1
                continue
            run = self._evaluate_and_record(
                opportunity,
                frozen,
                now=at,
                stale_after_seconds=self._stale_after_seconds,
            )
            evaluated += 1
            if run.evaluation.stale:
                stale += 1
                continue
            if run.trigger is None:
                if run.evaluation.met:
                    # The transition lost a race and the trigger insert was
                    # rolled back; the opportunity was never triggered.
                    skipped += 1
                continue
            if run.created:
                triggered += 1
                trigger_ids.append(run.trigger.trigger_id)
            else:
                skipped += 1
        return TriggerBatchOutcome(
            open=len(records),
            evaluated=evaluated,
            triggered=triggered,
            skipped=skipped,
            stale=stale,
            unavailable=unavailable,
            trigger_ids=tuple(trigger_ids),
            open_total=open_total,
            capped=capped,
        )

    # -- reads -----------------------------------------------------------

    def list(
        self,
        *,
        opportunity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        payloads: list[dict[str, Any]] = []
        for record in self._store.list_trigger_events(
            opportunity_id=opportunity_id,
            status=status,
            limit=limit,
            offset=offset,
        ):
            payload: dict[str, Any] = json.loads(record.payload_json)
            payloads.append(payload)
        return tuple(payloads)

    def count(self, *, opportunity_id: str | None = None, status: str | None = None) -> int:
        return self._store.count_trigger_events(opportunity_id=opportunity_id, status=status)

    def show(self, trigger_id: str) -> dict[str, Any]:
        record = self._store.get_trigger_event(trigger_id)
        if record is None:
            raise TriggerNotFound(trigger_id)
        payload: dict[str, Any] = json.loads(record.payload_json)
        return payload

    # -- internals -------------------------------------------------------

    def _evaluate_and_record(
        self,
        opportunity: Opportunity,
        frozen: TriggerSnapshot,
        *,
        now: str,
        stale_after_seconds: int,
    ) -> TriggerRun:
        evaluation = evaluate_trigger(
            opportunity.trigger_conditions,
            frozen,
            symbol=opportunity.symbol,
            now=now,
            stale_after_seconds=stale_after_seconds,
            snapshot_id=frozen.snapshot_id,
        )
        idempotency_key = (
            f"TRG-{opportunity.opportunity_id}-{evaluation.snapshot_hash[:16].upper()}"
        )
        existing = self._store.get_trigger_by_idempotency(idempotency_key)
        if existing is None and not evaluation.met:
            return TriggerRun(trigger=None, evaluation=evaluation, opportunity=opportunity)

        trigger_id = (
            existing.trigger_id if existing is not None else f"TRG-{_digest(idempotency_key)}"
        )
        record = TriggerEventRecord(
            trigger_id=trigger_id,
            opportunity_id=opportunity.opportunity_id,
            symbol=opportunity.symbol,
            status="PENDING_RECHECK",
            idempotency_key=idempotency_key,
            triggered_at=now,
            payload_json=json.dumps(
                _payload(
                    opportunity,
                    evaluation,
                    frozen,
                    now=now,
                    trigger_id=trigger_id,
                    idempotency_key=idempotency_key,
                ),
                sort_keys=True,
            ),
            snapshot_hash=evaluation.snapshot_hash,
            as_of=frozen.as_of,
            data_quality=evaluation.data_quality,
            condition_count=len(evaluation.results),
        )
        # The trigger insert and the OPEN -> TRIGGERED transition are one
        # ``BEGIN IMMEDIATE`` transaction. ``at=now`` is the trigger's
        # ``triggered_at``, so the trigger and the history entry always agree.
        updated = transition(
            opportunity,
            OpportunityState.TRIGGERED,
            at=now,
            reason_code="TRIGGER_MET",
            note=trigger_id,
        )
        opportunity_record = OpportunityRecord(
            opportunity_id=updated.opportunity_id,
            symbol=updated.symbol,
            state=updated.state.value,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            payload_json=json.dumps(updated.to_contract(), sort_keys=True),
        )
        event = OpportunityEventRecord(
            opportunity_id=updated.opportunity_id,
            seq=len(updated.state_history),
            at=now,
            from_state=opportunity.state.value,
            to_state=OpportunityState.TRIGGERED.value,
            reason_code="TRIGGER_MET",
            note=trigger_id,
        )
        status = self._store.save_trigger_and_transition(record, opportunity_record, event)
        if status == "inserted":
            record_audit(
                self._store,
                event_type="TRIGGER_CREATED",
                actor={"kind": "SYSTEM", "id": "trigger_evaluator"},
                subject={"kind": "TRIGGER", "id": trigger_id},
                payload={
                    "opportunity_id": opportunity.opportunity_id,
                    "snapshot_hash": evaluation.snapshot_hash,
                    "trigger_source": "CODE",
                },
                at=now,
            )
            return TriggerRun(
                trigger=record, evaluation=evaluation, opportunity=updated, created=True
            )
        if status == "existing":
            stored = self._store.get_trigger_by_idempotency(idempotency_key) or existing or record
            return TriggerRun(
                trigger=stored,
                evaluation=evaluation,
                opportunity=self._reload(opportunity),
                created=False,
            )
        # A concurrent writer moved the opportunity to a non-OPEN terminal
        # state (or removed it) before the transaction: the trigger insert was
        # rolled back, so this snapshot never produced a trigger.
        _LOGGER.warning(
            "trigger_transition_conflict",
            opportunity_id=opportunity.opportunity_id,
            trigger_id=trigger_id,
            reason="the opportunity is no longer OPEN",
        )
        return TriggerRun(
            trigger=None,
            evaluation=evaluation,
            opportunity=self._reload(opportunity),
            created=False,
        )

    def _reload(self, opportunity: Opportunity) -> Opportunity:
        try:
            return self._opportunities.show(opportunity.opportunity_id)
        except (OpportunityNotFound, OpportunityContractError):
            return opportunity

    def _unavailable_run(self, opportunity: Opportunity, now: str) -> TriggerRun:
        _LOGGER.warning(
            "trigger_snapshot_unavailable",
            opportunity_id=opportunity.opportunity_id,
            symbol=opportunity.symbol,
        )
        return TriggerRun(
            trigger=None,
            evaluation=_unavailable(now, "the source report snapshot is missing or unusable"),
            opportunity=opportunity,
        )

    def _existing_for(self, opportunity_id: str) -> TriggerEventRecord | None:
        matches = self._store.list_trigger_events(opportunity_id=opportunity_id, limit=1)
        return matches[0] if matches else None


def _payload(
    opportunity: Opportunity,
    evaluation: TriggerEvaluation,
    frozen: TriggerSnapshot,
    *,
    now: str,
    trigger_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "trigger_id": trigger_id,
        "opportunity_id": opportunity.opportunity_id,
        "symbol": opportunity.symbol,
        "side": opportunity.side,
        "environment": "PAPER",
        "triggered_at": now,
        "trigger_source": "CODE",
        "condition_results": [
            _condition_contract(result, fallback_at=now) for result in evaluation.results
        ],
        "snapshot": {
            "market_snapshot_id": evaluation.snapshot_id,
            "snapshot_hash": evaluation.snapshot_hash,
            "as_of": frozen.as_of,
            "data_quality": evaluation.data_quality,
        },
        "status": "PENDING_RECHECK",
        "entry_decision_id": None,
        "risk_verdict": None,
        "human_action": None,
        "idempotency_key": idempotency_key,
        "rejection_reason": None,
        "created_order_id": None,
    }


def _not_evaluated(at: str, reason: str) -> TriggerEvaluation:
    return TriggerEvaluation(
        met=False,
        stale=False,
        snapshot_id="",
        snapshot_hash=snapshot_hash({"reason": reason, "at": at}),
        as_of=at,
        data_quality="MISSING",
        results=(),
        reasons=(reason,),
    )


def _unavailable(at: str, reason: str) -> TriggerEvaluation:
    return TriggerEvaluation(
        met=False,
        stale=False,
        snapshot_id="",
        snapshot_hash=snapshot_hash({"reason": reason, "at": at}),
        as_of=at,
        data_quality="MISSING",
        results=(),
        reasons=(f"DATA_UNAVAILABLE: {reason}",),
    )


def trigger_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "trigger_id": payload["trigger_id"],
        "opportunity_id": payload["opportunity_id"],
        "symbol": payload["symbol"],
        "side": payload["side"],
        "status": payload["status"],
        "triggered_at": payload["triggered_at"],
        "trigger_source": payload["trigger_source"],
        "idempotency_key": payload["idempotency_key"],
        "created_order_id": payload["created_order_id"],
    }


__all__ = [
    "DEFAULT_BATCH_CAP",
    "DEFAULT_STALE_AFTER_SECONDS",
    "TriggerBatchOutcome",
    "TriggerError",
    "TriggerNotFound",
    "TriggerRun",
    "TriggerService",
    "trigger_summary",
]
