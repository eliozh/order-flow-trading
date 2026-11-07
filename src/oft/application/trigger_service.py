"""Manual trigger evaluation with snapshot freezing and idempotency.

Only code produces trigger events. The evaluator uses a frozen snapshot, records
the condition results and hash, and keeps one trigger per
``(opportunity, snapshot)`` via a unique idempotency key. The 15 s scheduler
arrives in a later slice; this is the manual path only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from oft.application.kill_switch import record_audit
from oft.application.opportunity_service import OpportunityService
from oft.domain.opportunities import Opportunity, OpportunityState
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerEvaluation, evaluate_trigger, snapshot_hash
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort, TriggerEventRecord

DEFAULT_STALE_AFTER_SECONDS = 60


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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()


class TriggerService:
    def __init__(self, *, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock
        self._opportunities = OpportunityService(store=store, clock=clock)

    def evaluate(
        self,
        opportunity_id: str,
        *,
        price: Decimal | None = None,
        as_of: str | None = None,
        snapshot_id: str | None = None,
        data_quality: str = "COMPLETE",
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
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

        if price is not None and as_of is not None:
            reference_price: Decimal = price
            snapshot_as_of = as_of
        else:
            report_price, report_as_of = self._report_mark(opportunity)
            reference_price = price if price is not None else report_price
            # The stale-snapshot guard must see when the frozen report mark was
            # taken, not the wall clock, or it can never fire.
            snapshot_as_of = as_of or report_as_of
        resolved_snapshot_id = (
            snapshot_id or f"SNAP-{_digest(f'{opportunity_id}:{snapshot_as_of}')}"
        )
        evaluation = evaluate_trigger(
            opportunity.trigger_conditions,
            symbol=opportunity.symbol,
            last_price=reference_price,
            snapshot_as_of=snapshot_as_of,
            now=now,
            stale_after_seconds=stale_after_seconds,
            data_quality=data_quality,
            snapshot_id=resolved_snapshot_id,
        )
        idempotency_key = f"TRG-{opportunity_id}-{evaluation.snapshot_hash[:16].upper()}"
        existing = self._store.get_trigger_by_idempotency(idempotency_key)
        if existing is not None:
            return TriggerRun(trigger=existing, evaluation=evaluation, opportunity=opportunity)
        if not evaluation.met:
            return TriggerRun(trigger=None, evaluation=evaluation, opportunity=opportunity)

        trigger_id = f"TRG-{_digest(idempotency_key)}"
        payload = {
            "schema_version": "1.0.0",
            "trigger_id": trigger_id,
            "opportunity_id": opportunity_id,
            "symbol": opportunity.symbol,
            "side": opportunity.side,
            "environment": "PAPER",
            "triggered_at": now,
            "trigger_source": "CODE",
            "condition_results": [result.to_contract() for result in evaluation.results],
            "snapshot": {
                "market_snapshot_id": resolved_snapshot_id,
                "snapshot_hash": evaluation.snapshot_hash,
                "as_of": snapshot_as_of,
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
        record = TriggerEventRecord(
            trigger_id=trigger_id,
            opportunity_id=opportunity_id,
            symbol=opportunity.symbol,
            status="PENDING_RECHECK",
            idempotency_key=idempotency_key,
            triggered_at=now,
            payload_json=json.dumps(payload, sort_keys=True),
        )
        inserted = self._store.save_trigger_event(record)
        if not inserted:
            duplicate = self._store.get_trigger_by_idempotency(idempotency_key)
            return TriggerRun(trigger=duplicate, evaluation=evaluation, opportunity=opportunity)
        triggered = self._opportunities.mark_triggered(opportunity_id, trigger_id=trigger_id)
        record_audit(
            self._store,
            event_type="TRIGGER_CREATED",
            actor={"kind": "SYSTEM", "id": "trigger_evaluator"},
            subject={"kind": "TRIGGER", "id": trigger_id},
            payload={
                "opportunity_id": opportunity_id,
                "snapshot_hash": evaluation.snapshot_hash,
                "trigger_source": "CODE",
            },
            at=now,
        )
        return TriggerRun(
            trigger=record, evaluation=evaluation, opportunity=triggered, created=True
        )

    def list(
        self, *, opportunity_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[dict[str, Any], ...]:
        payloads: list[dict[str, Any]] = []
        for record in self._store.list_trigger_events(
            opportunity_id=opportunity_id, limit=limit, offset=offset
        ):
            payload: dict[str, Any] = json.loads(record.payload_json)
            payloads.append(payload)
        return tuple(payloads)

    def count(self, *, opportunity_id: str | None = None) -> int:
        return self._store.count_trigger_events(opportunity_id=opportunity_id)

    def show(self, trigger_id: str) -> dict[str, Any]:
        record = self._store.get_trigger_event(trigger_id)
        if record is None:
            raise TriggerNotFound(trigger_id)
        payload: dict[str, Any] = json.loads(record.payload_json)
        return payload

    def _existing_for(self, opportunity_id: str) -> TriggerEventRecord | None:
        matches = self._store.list_trigger_events(opportunity_id=opportunity_id, limit=1)
        return matches[0] if matches else None

    def _report_mark(self, opportunity: Opportunity) -> tuple[Decimal, str]:
        report_id = str(opportunity.source.get("report_id", ""))
        report = self._store.get_report(report_id) if report_id else None
        if report is None:
            raise TriggerError("DATA_UNAVAILABLE", "the source report snapshot is missing")
        snapshot = json.loads(report.snapshot_json)
        value = snapshot.get("last_price") or snapshot.get("mark_price")
        if value is None:
            raise TriggerError("DATA_UNAVAILABLE", "the snapshot has no reference price")
        as_of = snapshot.get("as_of")
        if not isinstance(as_of, str) or not as_of:
            raise TriggerError("DATA_UNAVAILABLE", "the snapshot has no as_of timestamp")
        return Decimal(str(value)), as_of


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
    "DEFAULT_STALE_AFTER_SECONDS",
    "TriggerError",
    "TriggerNotFound",
    "TriggerRun",
    "TriggerService",
    "trigger_summary",
]
