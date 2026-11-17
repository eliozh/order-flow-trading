"""Deterministic trigger evaluation, snapshot freezing, and batch re-check queue.

Only code produces trigger events. The evaluator uses one frozen snapshot per
``(symbol, report_id)`` per tick, records the condition results and hash, and
keeps one trigger per ``(opportunity, snapshot_hash)`` via a unique idempotency
key. A stale or unavailable snapshot never triggers. A manual ``as_of`` override
is an equality assertion against the source report's ``as_of``; it can never
move the ceiling. No order, fill, position, or LLM call is made in this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from oft.application.kill_switch import record_audit
from oft.application.opportunity_service import (
    OpportunityNotFound,
    OpportunityService,
    _to_domain,
)
from oft.application.telegram_events import (
    NotificationEvent,
    NotificationSink,
    build_opportunity_burst_event,
    build_opportunity_triggered_event,
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
    """Counts from one batch evaluation of up to ``batch_cap`` OPEN opportunities."""

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


def _utc_instant(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC for comparison."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
        notifications: NotificationSink | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._opportunities = OpportunityService(
            store=store, clock=clock, notifications=notifications
        )
        self._provider = provider or ReportSnapshotProvider(store=store)
        self._stale_after_seconds = stale_after_seconds
        self._batch_cap = batch_cap
        self._notifications = notifications

    # -- manual path -----------------------------------------------------

    def evaluate(
        self,
        opportunity_id: str,
        *,
        price: Decimal | None = None,
        as_of: str | None = None,
        snapshot_id: str | None = None,
        stale_after_seconds: int | None = None,
    ) -> TriggerRun:
        """Evaluate one opportunity on demand, optionally with manual overrides.

        ``price`` replaces the frozen snapshot's ``last_price`` for the same
        instant. ``as_of`` is accepted only when it equals the source report's
        ``as_of`` after both are normalized to UTC: an earlier value would retain
        bars observed after the new ceiling (lookahead), and a later value would
        present the frozen series as fresher than it is, so either raises
        ``VALIDATION_FAILED``. ``snapshot_id`` may replace the frozen id. A STALE
        source is rejected on every override path; a missing or corrupt report is
        ``DATA_UNAVAILABLE`` rather than a hollow snapshot.
        """
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
        # Always resolve the frozen source report snapshot first, even when a
        # manual override is supplied. A STALE source can never be laundered
        # into a fresh snapshot, and a missing/corrupt report is unavailable
        # rather than a hollow price-only snapshot.
        frozen = self._provider.snapshot_for(opportunity)
        if frozen is None:
            return self._unavailable_run(opportunity, now)
        if snapshot_id is not None:
            frozen = replace(frozen, snapshot_id=snapshot_id)
        if price is None and as_of is None:
            return self._evaluate_and_record(
                opportunity,
                frozen,
                now=now,
                stale_after_seconds=budget,
            )
        if contract_data_quality(frozen.data_quality) == "STALE":
            # Never let a manual override launder a STALE report into a fresh
            # snapshot: the job path blocks it, and so must this one.
            reasons = "; ".join(frozen.quality_reasons) or "data quality is STALE"
            raise TriggerError(
                "VALIDATION_FAILED",
                f"cannot override a STALE source snapshot: {reasons}",
            )
        if as_of is not None:
            # ``as_of`` is an assertion, not a new ceiling: the preserved
            # series and metrics were only ever valid at the report's as_of.
            try:
                requested = _utc_instant(as_of)
                source = _utc_instant(frozen.as_of)
            except (TypeError, ValueError) as exc:
                raise TriggerError(
                    "VALIDATION_FAILED",
                    f"as_of override is not a valid ISO-8601 timestamp: {as_of!r}",
                ) from exc
            if requested != source:
                earlier = requested < source
                consequence = (
                    "an earlier as_of would retain bars after the new ceiling (lookahead)"
                    if earlier
                    else "a later as_of would present the stale series as fresh"
                )
                raise TriggerError(
                    "VALIDATION_FAILED",
                    f"as_of override {as_of} is "
                    f"{'earlier' if earlier else 'later'} than the source report as_of "
                    f"{frozen.as_of}: {consequence}; only an equal as_of may be supplied",
                )
        # Only the overridden fields change; every tick/series/metric, the as_of
        # ceiling and the source quality stay exactly as recorded in the report.
        if price is not None:
            frozen = replace(frozen, last_price=price)
        if frozen.last_price is None:
            return self._unavailable_run(opportunity, now)
        return self._evaluate_and_record(
            opportunity,
            frozen,
            now=now,
            stale_after_seconds=budget,
        )

    # -- batch/job path --------------------------------------------------

    def evaluate_all(self, now: str | None = None) -> TriggerBatchOutcome:
        """Expire due opportunities, then evaluate up to ``batch_cap`` OPEN ones.

        The batch is bounded by ``batch_cap``: when more OPEN opportunities
        exist, ``open_total``/``capped`` report the truncation and the rest are
        left for a later tick. Exactly one snapshot per ``(symbol,
        report_id)`` is fetched per tick. The batch is idempotent: an existing
        ``(opportunity, snapshot_hash)`` trigger is returned, never duplicated.
        A concurrent state transition is logged and skipped. The database is
        the queue, so a restart reconstructs it.
        """
        at = now or utc_timestamp(self._clock.now())
        self._opportunities.expire_due(at)
        records = self._store.list_opportunities(state="OPEN", limit=self._batch_cap)
        open_total = self._store.count_opportunities(state="OPEN")
        capped = open_total > len(records)
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
        created_runs: list[TriggerRun] = []
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
                notify=False,
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
                created_runs.append(run)
            else:
                skipped += 1
        self._notify_batch(created_runs)
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
        notify: bool = True,
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
            if notify:
                # Only a committed insert is a durable transition; this runs
                # strictly after the trigger/opportunity transaction committed.
                # Building and enqueueing share one guard: a renderer failure
                # must never escape after the ledger commit.
                self._safe_notify(
                    lambda: build_opportunity_triggered_event(
                        updated,
                        trigger_id=trigger_id,
                        triggered_at=now,
                        results=evaluation.results,
                    )
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
        # state (or removed it) before the transaction, or the insert hit an
        # unexpected constraint: the trigger insert was rolled back, so this
        # snapshot never produced a trigger.
        _LOGGER.warning(
            "trigger_transition_conflict",
            opportunity_id=opportunity.opportunity_id,
            trigger_id=trigger_id,
            reason=(
                "the opportunity is no longer OPEN or the trigger insert hit "
                "an unexpected constraint"
            ),
        )
        return TriggerRun(
            trigger=None,
            evaluation=evaluation,
            opportunity=self._reload(opportunity),
            created=False,
        )

    def _notify_batch(self, runs: Sequence[TriggerRun]) -> None:
        """One push for one new transition, or one coalesced burst for several.

        Coalescing happens only after the batch committed every transition, so
        the alert count reflects durable rows. Building and enqueueing are
        contained: a notification failure never affects the committed ledger.
        """
        if self._notifications is None or not runs:
            return
        try:
            events = [
                build_opportunity_triggered_event(
                    run.opportunity,
                    trigger_id=run.trigger.trigger_id,
                    triggered_at=run.trigger.triggered_at,
                    results=run.evaluation.results,
                )
                for run in runs
                if run.trigger is not None
            ]
            if len(events) == 1:
                self._notifications.enqueue_event(events[0])
            elif events:
                self._notifications.enqueue_event(build_opportunity_burst_event(events))
        except Exception as exc:  # noqa: BLE001 - never break the tick
            _LOGGER.warning(
                "trigger_alert_batch_failed",
                error_type=type(exc).__name__,
            )

    def _safe_notify(self, build: Callable[[], NotificationEvent]) -> None:
        """Build and enqueue one event inside a single containment guard.

        Both steps run after the ledger commit, so a builder/renderer exception
        must never escape into the trigger path. The toggle is still checked by
        ``NotificationService.enqueue_event``; building first is acceptable
        because the guard contains every failure.
        """
        if self._notifications is None:
            return
        try:
            event = build()
            self._notifications.enqueue_event(event)
        except Exception as exc:  # noqa: BLE001 - a push never breaks a trigger
            _LOGGER.warning(
                "trigger_alert_enqueue_failed",
                error_type=type(exc).__name__,
            )

    def _reload(self, opportunity: Opportunity) -> Opportunity:
        try:
            return self._opportunities.show(opportunity.opportunity_id)
        except (OpportunityNotFound, OpportunityContractError) as exc:
            # A lost race can delete or corrupt the row. Return the
            # pre-transition object so the caller still has a value, but never
            # hide the read failure.
            _LOGGER.warning(
                "trigger_reload_failed",
                opportunity_id=opportunity.opportunity_id,
                error=str(exc),
            )
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
