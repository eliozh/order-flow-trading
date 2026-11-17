"""The one write path for Telegram push notifications.

Pushes reuse the slice-47 outbox/receipt machinery: readiness is derived from the
existing channel state (never a network probe), exactly one allowlisted chat is
targeted, and the outbox's unique ``event_key`` makes duplicate completions an
idempotent no-op. Every settings, render, or store failure is caught and logged
here, so a Telegram problem can never change a scan, report, position, or audit
outcome.

Dependency direction stays acyclic: this module imports the pure event/render
helpers and the ports, never ``scan_service``, ``jobs``, or the API layer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from oft.application.scan_summary import (
    derive_scan_status,
    scan_cost_totals,
    stage_for_error_code,
)
from oft.application.telegram_events import (
    SCAN_SUMMARY_EVENT_TYPE,
    ScanSummary,
    build_scan_summary_event,
    scan_summary_dedupe_key,
)
from oft.application.telegram_messages import render_scan_summary
from oft.domain.telegram import MAX_DECIMAL_ADJUSTED_EXPONENT, MAX_SCAN_REPORT_IDS, ReceiptState
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import (
    JobRunRecord,
    ScanRecord,
    StorePort,
    TelegramOutboxRecord,
)

if TYPE_CHECKING:
    from oft.application.settings_service import TelegramAllowlistSettings
    from oft.application.telegram_events import NotificationEvent
    from oft.application.telegram_service import TelegramStatus

LOGGER = structlog.get_logger(__name__)

# The unconfigured delivery contract is shared by the scan/report payloads so
# the stable shape cannot drift between surfaces.
TELEGRAM_NOT_CONFIGURED_REASON = (
    "TELEGRAM_NOT_CONFIGURED: Telegram delivery is not configured in this build."
)
DELIVERED_REASON = "TELEGRAM_DELIVERED: the Telegram message was delivered."
DELIVERY_PENDING_REASON = "TELEGRAM_DELIVERY_PENDING: the Telegram message is queued for delivery."
DELIVERY_FAILED_REASON = "TELEGRAM_DELIVERY_FAILED: the Telegram message could not be delivered."
# A summary with no report ids is silent unless the failed-scan alert is on.
_SCAN_REPORT_TOGGLE = "scan_report"
_SCAN_FAILED_ALERT_TOGGLE = "scan_failed_alert"
_RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    SCAN_SUMMARY_EVENT_TYPE: render_scan_summary
}


class NotificationChannel(Protocol):
    """The TelegramService surface a push needs (structurally satisfied)."""

    def status_state(self) -> TelegramStatus: ...

    def allowlist(self) -> TelegramAllowlistSettings: ...


class NotificationSettings(Protocol):
    """The read-only push toggles a push needs."""

    def read_scan_report_push(self) -> bool: ...

    def read_failed_scan_alert(self) -> bool: ...


class NotificationService:
    """Enqueue at most one bounded, redacted Telegram push per event key."""

    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        channel: NotificationChannel,
        settings_service: NotificationSettings,
    ) -> None:
        self._store = store
        self._clock = clock
        self._channel = channel
        self._settings_service = settings_service

    # -- public write path -----------------------------------------------

    def enqueue(
        self,
        event_type: str,
        subject: Mapping[str, Any],
        dedupe_key: str,
        priority: int,
        payload: Mapping[str, Any],
        *,
        text: str | None = None,
    ) -> TelegramOutboxRecord | None:
        """Enqueue one push, or return ``None`` when it is not deliverable.

        ``subject`` documents the event's identity for callers; the outbox row
        is keyed by ``dedupe_key``. ``text`` is the pre-rendered message (the
        event builder renders once); when absent it is rendered here. On a
        duplicate ``event_key`` the store keeps the existing row and this
        returns that row, never a fabricated ``QUEUED`` record. No exception
        escapes: a failure here must never affect the caller's scan/report
        transaction.
        """
        try:
            renderer = _RENDERERS.get(event_type)
            if renderer is None:
                LOGGER.warning("telegram_notification_unknown_event", event_type=event_type)
                return None
            if not self._channel_ready():
                return None
            chat_id = self._target_chat()
            if chat_id is None:
                return None
            if not self._event_enabled(event_type, payload):
                return None
            rendered = text if text is not None else renderer(payload)
            if not isinstance(rendered, str) or not rendered:
                return None
            now = self._clock.now()
            timestamp = utc_timestamp(now)
            record = TelegramOutboxRecord(
                event_key=dedupe_key,
                kind=event_type,
                chat_id=chat_id,
                reply_to_message_id=None,
                payload_json=json.dumps({"text": rendered}),
                priority=priority,
                state=ReceiptState.QUEUED,
                attempts=0,
                next_attempt_at=timestamp,
                message_id=None,
                last_error_code=None,
                created_at=timestamp,
                sent_at=None,
            )
            if not self._store.append_telegram_outbox(record):
                return self._existing_outbox(dedupe_key)
            return record
        except Exception as exc:  # noqa: BLE001 - a push never breaks its caller
            LOGGER.warning(
                "telegram_notification_enqueue_failed",
                event_type=event_type,
                error_type=type(exc).__name__,
            )
            return None

    def enqueue_event(self, event: NotificationEvent) -> TelegramOutboxRecord | None:
        """Enqueue a prebuilt :class:`NotificationEvent` through ``enqueue``."""
        return self.enqueue(
            event.event_type,
            event.subject,
            event.dedupe_key,
            event.priority,
            event.payload,
            text=event.text,
        )

    def notify_scan(self, scan_id: str) -> TelegramOutboxRecord | None:
        """Enqueue the one summary for a completed scan, recomputed from rows.

        The summary is rebuilt from persisted state on every call, so a resume,
        retry, or second completion computes the same dedupe key and the store's
        unique constraint makes the send single-shot. Every settings, store, and
        render failure is caught here, so this never raises into a scan path.
        """
        try:
            summary = self._scan_summary(scan_id)
            if summary is None:
                return None
            return self.enqueue_event(build_scan_summary_event(summary))
        except Exception as exc:  # noqa: BLE001 - never break the scan path
            LOGGER.warning(
                "telegram_scan_summary_unavailable",
                scan_id=scan_id,
                error_type=type(exc).__name__,
            )
            return None

    # -- readiness and targeting -----------------------------------------

    def _channel_ready(self) -> bool:
        return self._channel.status_state().state == "READY"

    def _target_chat(self) -> int | None:
        chat_ids = tuple(
            chat_id
            for chat_id in self._channel.allowlist().chat_ids
            if isinstance(chat_id, int) and not isinstance(chat_id, bool)
        )
        if not chat_ids:
            return None
        return min(chat_ids)

    def _event_enabled(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if event_type != SCAN_SUMMARY_EVENT_TYPE:
            return False
        report_ids = payload.get("report_ids") or ()
        if not report_ids:
            return payload.get("status") == "FAILED" and self._read_toggle(
                _SCAN_FAILED_ALERT_TOGGLE
            )
        return self._read_toggle(_SCAN_REPORT_TOGGLE)

    def _read_toggle(self, toggle: str) -> bool:
        try:
            if toggle == _SCAN_REPORT_TOGGLE:
                return bool(self._settings_service.read_scan_report_push())
            return bool(self._settings_service.read_failed_scan_alert())
        except Exception as exc:  # noqa: BLE001 - fail closed on an unreadable toggle
            LOGGER.warning(
                "telegram_notification_toggle_unreadable",
                toggle=toggle,
                error_type=type(exc).__name__,
            )
            return False

    def _existing_outbox(self, event_key: str) -> TelegramOutboxRecord | None:
        """The stored row for ``event_key`` after a duplicate insert (or ``None``)."""
        try:
            return self._store.get_telegram_outbox(event_key)
        except Exception:  # noqa: BLE001 - a duplicate is still a no-op
            return None

    # -- bounded summary loader ------------------------------------------

    def _scan_summary(self, scan_id: str) -> ScanSummary | None:
        rows = tuple(self._store.list_scan_symbols(scan_id))
        run = _job_run(self._store, scan_id)
        # One shared derivation: a terminal FAILED run dominates and terminal
        # row stages merge as FAILED > PARTIAL > COMPLETE.
        status = derive_scan_status(rows, run.status if run is not None else None)
        if status is None:
            return None
        all_report_ids = tuple(sorted({row.report_id for row in rows if row.report_id}))
        cost, unpriced = scan_cost_totals(rows)
        cost_text = _bounded_cost(cost)
        if cost_text == "N/A":
            unpriced = True
        trigger_source, period = _scan_context(self._store, scan_id, run)
        failed_row = next((row for row in rows if row.stage == "FAILED"), None)
        reason_code = failed_row.error_code if failed_row is not None else None
        if reason_code is None and run is not None:
            reason_code = run.error_code
        failed_stage = (
            (failed_row.failed_stage or stage_for_error_code(reason_code))
            if failed_row is not None
            else (stage_for_error_code(reason_code) if reason_code else None)
        )
        if status != "FAILED":
            failed_stage, reason_code = None, None
        finished_at = run.finished_at if run is not None else None
        generated_at = (
            finished_at
            if isinstance(finished_at, str) and finished_at.strip()
            else utc_timestamp(self._clock.now())
        )
        return ScanSummary(
            scan_id=scan_id,
            status=status,
            trigger_source=trigger_source,
            period=period,
            symbols_total=len(rows),
            symbols_completed=sum(1 for row in rows if row.stage == "DONE"),
            symbols_degraded=sum(1 for row in rows if row.stage == "DEGRADED"),
            symbols_failed=sum(1 for row in rows if row.stage == "FAILED"),
            new_opportunities=_new_opportunities(self._store, all_report_ids),
            cost_usd=cost_text,
            cost_unpriced=unpriced,
            report_ids=all_report_ids[:MAX_SCAN_REPORT_IDS],
            reports_total=len(all_report_ids),
            failed_stage=failed_stage,
            reason_code=reason_code,
            generated_at=generated_at,
        )


def delivery_contract(
    store: StorePort,
    scan_id: str,
    *,
    fallback_scan_id: str | None = None,
) -> dict[str, Any]:
    """The scan/report ``delivery`` value: a real receipt when one exists.

    The unconfigured shape is returned byte-for-byte when no summary was
    enqueued, so existing CLI/web payload fixtures stay valid. A found receipt
    adds its identity and state without removing ``configured``/``reason``.
    """
    record = _lookup_receipt(store, scan_summary_dedupe_key(scan_id))
    if record is None and fallback_scan_id and fallback_scan_id != scan_id:
        record = _lookup_receipt(store, scan_summary_dedupe_key(fallback_scan_id))
    if record is None:
        return not_configured_delivery()
    state = str(record.state)
    return {
        "configured": True,
        "reason": _delivery_reason(state),
        "event_key": record.event_key,
        "state": state,
        "attempts": record.attempts,
        "message_id": record.message_id,
        "last_error_code": record.last_error_code,
        "created_at": record.created_at,
        "sent_at": record.sent_at,
    }


def not_configured_delivery() -> dict[str, Any]:
    """The exact legacy stub; one constant for every unconfigured surface."""
    return {"configured": False, "reason": TELEGRAM_NOT_CONFIGURED_REASON}


def _delivery_reason(state: str) -> str:
    if state == "sent":
        return DELIVERED_REASON
    if state == "failed":
        return DELIVERY_FAILED_REASON
    return DELIVERY_PENDING_REASON


def _lookup_receipt(store: StorePort, event_key: str) -> TelegramOutboxRecord | None:
    try:
        record = store.get_telegram_outbox(event_key)
    except Exception:  # noqa: BLE001 - a receipt read never breaks a payload
        return None
    return record if isinstance(record, TelegramOutboxRecord) else None


def _job_run(store: StorePort, scan_id: str) -> JobRunRecord | None:
    try:
        return store.get_job_run(scan_id)
    except Exception:  # noqa: BLE001 - tiny test stores may omit this read
        return None


def _bounded_cost(cost: Decimal) -> str:
    """Render the total cost with the renderer's hostile-exponent guard.

    ``1e100000000`` would materialize a ~100 MB string before the renderer's
    length clamp; reject it up front and let the caller mark the total unpriced.
    """
    if cost == 0:
        return "0"
    if not cost.is_finite():
        return "N/A"
    if abs(cost.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT:
        return "N/A"
    return format(cost, "f")


def _new_opportunities(store: StorePort, report_ids: Sequence[str]) -> int:
    seen: set[str] = set()
    for report_id in report_ids:
        try:
            report = store.get_report(report_id)
        except Exception:  # noqa: BLE001 - one unreadable report is skipped
            continue
        if report is None or not isinstance(report.llm_json, str) or not report.llm_json:
            continue
        try:
            value = json.loads(report.llm_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, Mapping):
            continue
        ids = value.get("opportunity_ids")
        if not isinstance(ids, Sequence) or isinstance(ids, str):
            continue
        for item in ids:
            if isinstance(item, str) and item:
                seen.add(item)
    return len(seen)


def _scan_context(store: StorePort, scan_id: str, run: JobRunRecord | None) -> tuple[str, str]:
    trigger = run.trigger_source if run is not None else None
    period = run.period if run is not None else None
    child = _safe_scan(store, scan_id)
    if child is not None:
        trigger = trigger or child.trigger_source
        period = period or child.period
    if not trigger or not period:
        for item in _safe_batch(store, scan_id):
            trigger = trigger or item.trigger_source
            period = period or item.period
            if trigger and period:
                break
    return str(trigger or "MANUAL"), str(period or "4h")


def _safe_scan(store: StorePort, scan_id: str) -> ScanRecord | None:
    try:
        return store.get_scan(scan_id)
    except Exception:  # noqa: BLE001 - context is best-effort
        return None


def _safe_batch(store: StorePort, scan_id: str) -> tuple[ScanRecord, ...]:
    try:
        return tuple(store.list_scans_by_batch(scan_id))
    except Exception:  # noqa: BLE001 - context is best-effort
        return ()


__all__ = [
    "DELIVERED_REASON",
    "DELIVERY_FAILED_REASON",
    "DELIVERY_PENDING_REASON",
    "NotificationChannel",
    "NotificationService",
    "NotificationSettings",
    "TELEGRAM_NOT_CONFIGURED_REASON",
    "delivery_contract",
    "not_configured_delivery",
]
