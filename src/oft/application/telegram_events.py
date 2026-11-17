"""Pure scan-summary event values for the Telegram outbox (no I/O).

A notification payload is the *only* data a push may carry. This module builds
the scan-completion payload from an explicit allowlist of fields, so an error
message, stored narrative, prompt, provider body, filesystem path, or token can
never reach the outbox. Rendering happens in :mod:`oft.application.telegram_messages`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oft.application.scan_summary import stage_for_error_code
from oft.application.telegram_messages import render_scan_summary
from oft.domain.telegram import MAX_SCAN_REPORT_IDS

SCAN_SUMMARY_EVENT_TYPE = "scan_summary"
SCAN_SUMMARY_PRIORITY = 10
SCAN_SUBJECT_KIND = "SCAN"
SCAN_STATUSES = ("COMPLETE", "PARTIAL", "FAILED")


@dataclass(frozen=True)
class NotificationEvent:
    """One fully-built push event: stable identity plus its rendered text."""

    event_type: str
    subject: dict[str, Any]
    dedupe_key: str
    priority: int
    payload: dict[str, Any]
    text: str


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


def _non_negative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


__all__ = [
    "MAX_SCAN_REPORT_IDS",
    "SCAN_STATUSES",
    "SCAN_SUBJECT_KIND",
    "SCAN_SUMMARY_EVENT_TYPE",
    "SCAN_SUMMARY_PRIORITY",
    "NotificationEvent",
    "ScanSummary",
    "build_scan_summary_event",
    "scan_summary_dedupe_key",
    "scan_summary_subject",
    "stage_for_error_code",
]
