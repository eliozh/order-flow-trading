"""Pure notification-event builders for the Telegram outbox (no I/O).

A notification payload is the *only* data a push may carry. This module builds
every push family — scan-summary completion, opportunity TRIGGERED, opportunity
INVALIDATED, coalesced TRIGGERED bursts, and entry-re-check-awaiting-human — from
an explicit allowlist of fields, so an error message, stored narrative, thesis,
rationale, prompt, provider/model text, filesystem path, or token can never
reach the outbox. Each builder also derives a stable per-transition dedupe key,
so a replay is an outbox no-op. Rendering happens in
:mod:`oft.application.telegram_messages`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from oft.application.scan_summary import stage_for_error_code
from oft.application.telegram_messages import (
    render_entry_recheck_alert,
    render_invalidation_alert,
    render_opportunity_burst,
    render_scan_summary,
    render_trigger_alert,
)
from oft.domain.opportunities import Condition, Opportunity
from oft.domain.telegram import MAX_OPPORTUNITY_ALERT_ITEMS, MAX_SCAN_REPORT_IDS
from oft.domain.trigger import ConditionResult

SCAN_SUMMARY_EVENT_TYPE = "scan_summary"
SCAN_SUMMARY_PRIORITY = 10
SCAN_SUBJECT_KIND = "SCAN"
SCAN_STATUSES = ("COMPLETE", "PARTIAL", "FAILED")

OPPORTUNITY_TRIGGERED_EVENT_TYPE = "opportunity_triggered"
OPPORTUNITY_INVALIDATED_EVENT_TYPE = "opportunity_invalidated"
OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE = "opportunity_triggered_burst"
ENTRY_RECHECK_EVENT_TYPE = "entry_recheck"
# A trigger/burst is more urgent than a routine scan summary; an entry re-check
# awaiting a human action is the most urgent of all.
OPPORTUNITY_ALERT_PRIORITY = 20
ENTRY_RECHECK_PRIORITY = 30
OPPORTUNITY_SUBJECT_KIND = "OPPORTUNITY"
ENTRY_SUBJECT_KIND = "TRIGGER"
BURST_SUBJECT_KIND = "OPPORTUNITY_BURST"
_OPPORTUNITY_ALERT_STATE_TRIGGERED = "TRIGGERED"
_OPPORTUNITY_ALERT_STATE_INVALIDATED = "INVALIDATED"
# The five keys an invalidation condition may carry a level in; the first
# present one is the displayed level.
_LEVEL_KEYS = ("price", "price_low", "price_high", "threshold", "value")
# Entry decisions are directional in the ledger; the phone alert reports the
# action ("ENTER") while the direction is already named by ``side``.
_ENTRY_ALERT_DECISIONS = {
    "ENTER_LONG": "ENTER",
    "ENTER_SHORT": "ENTER",
    "NO_ENTRY": "NO_ENTRY",
}


@dataclass(frozen=True)
class NotificationEvent:
    """One fully-built push event: stable identity plus its rendered text."""

    event_type: str
    subject: dict[str, Any]
    dedupe_key: str
    priority: int
    payload: dict[str, Any]
    text: str


class NotificationSink(Protocol):
    """The one write a service needs from the notification layer.

    Services depend on this narrow structural surface instead of the concrete
    ``NotificationService`` so a notification failure can never couple back into
    a ledger transaction. ``NotificationService`` satisfies it.
    """

    def enqueue_event(self, event: NotificationEvent) -> object | None: ...


@dataclass(frozen=True)
class ScanSummary:
    """The allowlisted values of one scan-completion notification.

    Every field is rendered as data. ``error_message`` is deliberately absent:
    a failure is described by its stage and a stable reason code only.
    """

    scan_id: str
    status: str
    trigger_source: str
    period: str
    symbols_total: int
    symbols_completed: int
    symbols_degraded: int
    symbols_failed: int
    new_opportunities: int
    cost_usd: str
    cost_unpriced: bool
    report_ids: tuple[str, ...]
    reports_total: int
    failed_stage: str | None
    reason_code: str | None
    generated_at: str


def scan_summary_dedupe_key(scan_id: str) -> str:
    """The idempotency key for one scan's summary; one scan, at most one push."""
    return f"scan:{scan_id}:summary"


def scan_summary_subject(scan_id: str) -> dict[str, Any]:
    return {"kind": SCAN_SUBJECT_KIND, "id": scan_id}


def build_scan_summary_event(summary: ScanSummary) -> NotificationEvent:
    """Build the redacted scan-summary payload and its rendered text."""
    status = summary.status if summary.status in SCAN_STATUSES else "FAILED"
    report_ids = tuple(sorted({report_id for report_id in summary.report_ids if report_id}))[
        :MAX_SCAN_REPORT_IDS
    ]
    # ``report_ids`` is bounded for the message; the total keeps the count honest.
    reports_total = max(_non_negative(summary.reports_total), len(report_ids))
    payload: dict[str, Any] = {
        "status": status,
        "scan_id": summary.scan_id,
        "trigger_source": summary.trigger_source,
        "period": summary.period,
        "symbols_total": _non_negative(summary.symbols_total),
        "symbols_completed": _non_negative(summary.symbols_completed),
        "symbols_degraded": _non_negative(summary.symbols_degraded),
        "symbols_failed": _non_negative(summary.symbols_failed),
        "new_opportunities": _non_negative(summary.new_opportunities),
        "cost_usd": summary.cost_usd,
        "cost_unpriced": bool(summary.cost_unpriced),
        "report_ids": list(report_ids),
        "reports_total": reports_total,
        "failed_stage": (
            stage_for_error_code(summary.reason_code)
            if summary.failed_stage is None and summary.reason_code is not None
            else summary.failed_stage
        ),
        "reason_code": summary.reason_code,
        "generated_at": summary.generated_at,
    }
    return NotificationEvent(
        event_type=SCAN_SUMMARY_EVENT_TYPE,
        subject=scan_summary_subject(summary.scan_id),
        dedupe_key=scan_summary_dedupe_key(summary.scan_id),
        priority=SCAN_SUMMARY_PRIORITY,
        payload=payload,
        text=render_scan_summary(payload),
    )


def entry_alert_decision(value: object) -> str | None:
    """Normalize one ledger entry decision to the phone alert's action word."""
    return _ENTRY_ALERT_DECISIONS.get(value) if isinstance(value, str) else None


def opportunity_state_version(opportunity: Opportunity) -> int:
    """The 1-based version of the opportunity's current state.

    The append-only history is the durable authority: OPEN creation is version
    1, TRIGGERED is 2, and INVALIDATED after TRIGGERED is 3. A transition makes
    the ``(opportunity_id, state, version)`` triple unique even across retries.
    """
    return len(opportunity.state_history)


def opportunity_transition_dedupe_key(opportunity_id: str, state: str, version: int) -> str:
    return f"opp:{opportunity_id}:{state}:{version}"


def build_opportunity_triggered_event(
    opportunity: Opportunity,
    *,
    trigger_id: str,
    triggered_at: str,
    results: Sequence[ConditionResult],
) -> NotificationEvent:
    """Build the TRIGGERED push from an explicit, redacted allowlist.

    Thesis, prompts, provider bodies, notes, and paths are never copied: only
    the signal levels the phone needs. ``state_version`` makes the event unique
    per durable transition, so a replay is an outbox no-op.
    """
    version = opportunity_state_version(opportunity)
    payload: dict[str, Any] = {
        "opportunity_id": opportunity.opportunity_id,
        "symbol": opportunity.symbol,
        "side": opportunity.side,
        "state": _OPPORTUNITY_ALERT_STATE_TRIGGERED,
        "state_version": version,
        "confidence": opportunity.confidence,
        "zone_low": str(opportunity.zone_low),
        "zone_high": str(opportunity.zone_high),
        "trigger_id": trigger_id,
        "triggered_at": triggered_at,
        "trigger_levels": [
            {
                "condition_id": result.condition_id,
                "type": result.type,
                "observed_value": result.observed_value,
                "threshold": result.threshold,
            }
            for result in results
        ],
        "invalidation_levels": [
            {
                "condition_id": condition.condition_id,
                "type": condition.type,
                "level": _condition_level(condition),
            }
            for condition in opportunity.invalidation_conditions[:MAX_OPPORTUNITY_ALERT_ITEMS]
        ],
    }
    return NotificationEvent(
        event_type=OPPORTUNITY_TRIGGERED_EVENT_TYPE,
        subject={"kind": OPPORTUNITY_SUBJECT_KIND, "id": opportunity.opportunity_id},
        dedupe_key=opportunity_transition_dedupe_key(
            opportunity.opportunity_id, _OPPORTUNITY_ALERT_STATE_TRIGGERED, version
        ),
        priority=OPPORTUNITY_ALERT_PRIORITY,
        payload=payload,
        text=render_trigger_alert(payload),
    )


def build_opportunity_invalidated_event(
    opportunity: Opportunity,
    *,
    reason_code: str,
    invalidated_at: str,
) -> NotificationEvent:
    """Build the INVALIDATED push carrying only the stable reason code.

    The free-text note is intentionally absent so operator prose, paths, or
    provider output can never reach the outbox.
    """
    version = opportunity_state_version(opportunity)
    payload: dict[str, Any] = {
        "opportunity_id": opportunity.opportunity_id,
        "symbol": opportunity.symbol,
        "side": opportunity.side,
        "state": _OPPORTUNITY_ALERT_STATE_INVALIDATED,
        "state_version": version,
        "reason_code": reason_code,
        "invalidated_at": invalidated_at,
    }
    return NotificationEvent(
        event_type=OPPORTUNITY_INVALIDATED_EVENT_TYPE,
        subject={"kind": OPPORTUNITY_SUBJECT_KIND, "id": opportunity.opportunity_id},
        dedupe_key=opportunity_transition_dedupe_key(
            opportunity.opportunity_id, _OPPORTUNITY_ALERT_STATE_INVALIDATED, version
        ),
        priority=OPPORTUNITY_ALERT_PRIORITY,
        payload=payload,
        text=render_invalidation_alert(payload),
    )


def build_entry_recheck_event(
    *,
    trigger_id: str,
    opportunity_id: str,
    symbol: str,
    side: str,
    decision_id: str,
    decision: str,
    confidence: float | None,
    generated_at: str,
) -> NotificationEvent:
    """Build the "awaiting human action" entry push.

    The payload is an allowlist: no rationale, prompt, provider/model text, or
    risk internals. The desktop/CLI instruction is not a payload field; the
    renderer emits its own constant, so no builder can widen what is shown.
    """
    payload: dict[str, Any] = {
        "trigger_id": trigger_id,
        "opportunity_id": opportunity_id,
        "symbol": symbol,
        "side": side,
        "decision_id": decision_id,
        "decision": entry_alert_decision(decision) or decision,
        "confidence": confidence,
        "generated_at": generated_at,
    }
    return NotificationEvent(
        event_type=ENTRY_RECHECK_EVENT_TYPE,
        subject={"kind": ENTRY_SUBJECT_KIND, "id": trigger_id},
        dedupe_key=f"entry:{trigger_id}:{decision_id}",
        priority=ENTRY_RECHECK_PRIORITY,
        payload=payload,
        text=render_entry_recheck_alert(payload),
    )


def build_opportunity_burst_event(events: Sequence[NotificationEvent]) -> NotificationEvent:
    """Coalesce several TRIGGERED transitions into one bounded push.

    The dedupe key is derived deterministically from the sorted per-transition
    keys, so the same tick produces the same row even if the in-memory order of
    the batch changes. The item list is bounded; the remainder is a count plus a
    ``/opportunities`` pointer.
    """
    ordered = tuple(events)
    if not ordered:
        raise ValueError("a burst requires at least one trigger event")
    digest = hashlib.sha256(
        "|".join(sorted(event.dedupe_key for event in ordered)).encode("utf-8")
    ).hexdigest()[:16]
    items = [
        {
            "symbol": event.payload.get("symbol"),
            "side": event.payload.get("side"),
            "confidence": event.payload.get("confidence"),
            "opportunity_id": event.payload.get("opportunity_id"),
        }
        for event in ordered[:MAX_OPPORTUNITY_ALERT_ITEMS]
    ]
    payload: dict[str, Any] = {
        "count": len(ordered),
        "items": items,
        "remaining": max(0, len(ordered) - len(items)),
    }
    return NotificationEvent(
        event_type=OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE,
        subject={"kind": BURST_SUBJECT_KIND, "id": digest},
        dedupe_key=f"opp-burst:TRIGGERED:{digest}",
        priority=OPPORTUNITY_ALERT_PRIORITY,
        payload=payload,
        text=render_opportunity_burst(payload),
    )


def _condition_level(condition: Condition) -> str | None:
    """The first present level parameter of one invalidation condition."""
    for key in _LEVEL_KEYS:
        value = condition.params.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _non_negative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


__all__ = [
    "ENTRY_RECHECK_EVENT_TYPE",
    "ENTRY_RECHECK_PRIORITY",
    "MAX_OPPORTUNITY_ALERT_ITEMS",
    "MAX_SCAN_REPORT_IDS",
    "OPPORTUNITY_ALERT_PRIORITY",
    "OPPORTUNITY_INVALIDATED_EVENT_TYPE",
    "OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE",
    "OPPORTUNITY_TRIGGERED_EVENT_TYPE",
    "SCAN_STATUSES",
    "SCAN_SUBJECT_KIND",
    "SCAN_SUMMARY_EVENT_TYPE",
    "SCAN_SUMMARY_PRIORITY",
    "NotificationEvent",
    "NotificationSink",
    "ScanSummary",
    "build_entry_recheck_event",
    "build_opportunity_burst_event",
    "build_opportunity_invalidated_event",
    "build_opportunity_triggered_event",
    "build_scan_summary_event",
    "entry_alert_decision",
    "opportunity_state_version",
    "opportunity_transition_dedupe_key",
    "scan_summary_dedupe_key",
    "scan_summary_subject",
    "stage_for_error_code",
]
