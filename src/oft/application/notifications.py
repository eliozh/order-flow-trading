"""The one write path for Telegram push notifications.

Pushes reuse the slice-47 outbox/receipt machinery: readiness is derived from the
existing channel state (never a network probe), the outbox's unique ``event_key``
makes duplicate completions an idempotent no-op, and every settings, render, or
store failure is caught and logged here, so a Telegram problem can never change
a scan, report, position, or audit outcome.

Automatic pushes are **private-chat-only**. A push is enqueued without a user
action, so it has no per-sender ``is_authorized`` check: targeting a group or
supergroup would expose scan data to every member of that chat. Targeting
therefore selects a push chat only when the allowlist contains exactly one
positive (private) chat id, and fails closed (no push) when it contains none or
several. A negative group/supergroup id is never a push target.

Dependency direction stays acyclic: this module imports the pure event/render
helpers and the ports, never ``scan_service``, ``jobs``, or the API layer.

Documented maintainability exception: this module sits just over the 800-line
soft ceiling because it is the single push write path - readiness, private-only
targeting, per-event toggles, cooldown/dedupe markers, and the bounded summary
loader all live here so a Telegram failure can never couple into another
service. The budget-alert hand-off is a one-line delegation to the pure
``telegram_events`` helper. Deferred: extract the per-event toggle/targeting
policy into a companion module when the alert surface next changes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from oft.application.scan_summary import (
    derive_scan_status,
    format_cost_total,
    scan_cost_totals,
    stage_for_error_code,
)
from oft.application.telegram_events import (
    BUDGET_THRESHOLD_EVENT_TYPE,
    DEGRADATION_EVENT_TYPE,
    DEGRADATION_STATES,
    ENTRY_RECHECK_EVENT_TYPE,
    OPPORTUNITY_INVALIDATED_EVENT_TYPE,
    OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE,
    OPPORTUNITY_TRIGGERED_EVENT_TYPE,
    POSITION_MANAGEMENT_EVENT_TYPE,
    RISK_ALERT_EVENT_TYPE,
    SCAN_SUMMARY_EVENT_TYPE,
    ScanSummary,
    build_degradation_event,
    build_scan_summary_event,
    degradation_dedupe_key,
    enqueue_budget_threshold,
    scan_summary_dedupe_key,
)
from oft.application.telegram_messages import (
    render_budget_alert,
    render_degradation_alert,
    render_entry_recheck_alert,
    render_invalidation_alert,
    render_opportunity_burst,
    render_position_management_alert,
    render_risk_alert,
    render_scan_summary,
    render_trigger_alert,
)
from oft.domain.telegram import MAX_SCAN_REPORT_IDS, ReceiptState
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
_TRIGGER_ALERT_TOGGLE = "trigger_alert"
_ENTRY_RECHECK_ALERT_TOGGLE = "entry_recheck_alert"
_POSITION_PUSH_TOGGLE = "position"
_RISK_PUSH_TOGGLE = "risk"
_BUDGET_PUSH_TOGGLE = "budget"
# The durable marker prefix for one cooldown/dedupe key; the value is the last
# alerted period so a restart or a repeated tick is a no-op.
ALERT_COOLDOWN_STATE_PREFIX = "telegram.alert_cooldown."
# The last DEGRADED/RECOVERED state per subject, persisted as
# ``"STATE:sequence"`` so a daemon restart inside one episode neither re-emits
# DEGRADED nor drops the one RECOVERED, while a later episode is distinct.
DEGRADATION_STATE_PREFIX = "telegram.degradation_state."
_RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    SCAN_SUMMARY_EVENT_TYPE: render_scan_summary,
    OPPORTUNITY_TRIGGERED_EVENT_TYPE: render_trigger_alert,
    OPPORTUNITY_INVALIDATED_EVENT_TYPE: render_invalidation_alert,
    OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE: render_opportunity_burst,
    ENTRY_RECHECK_EVENT_TYPE: render_entry_recheck_alert,
    POSITION_MANAGEMENT_EVENT_TYPE: render_position_management_alert,
    RISK_ALERT_EVENT_TYPE: render_risk_alert,
    BUDGET_THRESHOLD_EVENT_TYPE: render_budget_alert,
    DEGRADATION_EVENT_TYPE: render_degradation_alert,
}


class _OutboxProbeUnknown:
    """Sentinel: the outbox probe failed, so row existence is unknown."""

    __slots__ = ()


_OUTBOX_PROBE_UNKNOWN = _OutboxProbeUnknown()


class NotificationChannel(Protocol):
    """The TelegramService surface a push needs (structurally satisfied)."""

    def status_state(self) -> TelegramStatus: ...

    def allowlist(self) -> TelegramAllowlistSettings: ...


class NotificationSettings(Protocol):
    """The read-only push toggles a push needs."""

    def read_scan_report_push(self) -> bool: ...

    def read_failed_scan_alert(self) -> bool: ...

    def read_trigger_alert(self) -> bool: ...

    def read_entry_recheck_alert(self) -> bool: ...

    def read_position_push(self) -> bool: ...

    def read_risk_push(self) -> bool: ...

    def read_budget_push(self) -> bool: ...


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
                existing = self._existing_outbox(dedupe_key)
                # An unreadable probe is "unknown", not a row: this path still
                # reports a duplicate as ``None`` and never leaks the sentinel.
                return existing if isinstance(existing, TelegramOutboxRecord) else None
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

    def enqueue_cooled(
        self, event: NotificationEvent, *, cooldown_key: str, period: str
    ) -> TelegramOutboxRecord | None:
        """Enqueue at most once per ``cooldown_key`` and ``period``.

        The marker is persisted in ``system_state``, so a repeated tick, a
        process restart, or a second caller cannot re-alert within the same
        period. A new period alerts again. An unreadable marker fails closed
        (no message) and a failed enqueue leaves no marker, so a later tick can
        retry. No exception escapes: a push never breaks its caller.
        """
        marker = alert_cooldown_state_key(cooldown_key)
        try:
            if self._store.get_system_state(marker) == period:
                return None
        except Exception as exc:  # noqa: BLE001 - fail closed on an unreadable marker
            LOGGER.warning(
                "telegram_alert_cooldown_unreadable",
                cooldown_key=cooldown_key,
                error_type=type(exc).__name__,
            )
            return None
        record = self.enqueue_event(event)
        if record is None:
            return None
        try:
            self._store.set_system_state(
                marker, period, updated_at=utc_timestamp(self._clock.now())
            )
        except Exception as exc:  # noqa: BLE001 - the outbox key still dedupes
            LOGGER.warning(
                "telegram_alert_cooldown_unwritable",
                cooldown_key=cooldown_key,
                error_type=type(exc).__name__,
            )
        return record

    def enqueue_degradation_transition(
        self, *, subject: str, state: str, reason_code: str, occurred_at: str
    ) -> TelegramOutboxRecord | None:
        """Enqueue one DEGRADED/RECOVERED transition per durable episode.

        The last state *and an advancing episode sequence* are persisted in
        ``system_state`` as ``"STATE:sequence"``. That gives the outbox key two
        required properties at once:

        * A restart mid-episode re-observing the same state does not enqueue
          another DEGRADED.
        * A retry of the same transition (including after a failed marker
          write) recomputes the same episode token, so the outbox unique key
          dedupes it instead of emitting a duplicate.
        * A genuinely new episode after RECOVERED gets the next sequence and
          therefore a distinct key, so the next outage is not silently dropped.
        * A lost marker (a failed write in either direction) is recognized from
          the outbox record of the episode and healed, so a recovery is never
          mistaken for a clean install and a new outage is never mistaken for a
          restart re-observation.

        A RECOVERED with no marker and no queued DEGRADED in the outbox is a
        clean install and enqueues nothing; there is nothing to recover from.
        A failed enqueue leaves no marker, so a later transition can retry. No
        exception escapes.
        """
        if state not in DEGRADATION_STATES:
            return None
        marker = degradation_state_key(subject)
        try:
            previous_raw = self._store.get_system_state(marker)
        except Exception as exc:  # noqa: BLE001 - fail closed on unreadable state
            LOGGER.warning(
                "telegram_degradation_state_unreadable",
                subject=subject,
                error_type=type(exc).__name__,
            )
            return None
        previous_state, previous_sequence = parse_degradation_marker(previous_raw)
        sequence = previous_sequence + 1
        marker_lost = False
        # Probe boundary: terminal outbox rows live only 7 days
        # (TELEGRAM_OUTBOX_RETENTION_SECONDS), so an episode whose marker writes
        # both fail beyond that window is a documented limitation of this heal.
        if previous_state == state:
            # Same state as the marker: either a restart/repeat re-observation
            # (a no-op) or a lost marker after the opposite transition was
            # already enqueued. The outbox is the durable transition record, so
            # probe the opposite state at the next sequence to tell them apart.
            opposite = "RECOVERED" if state == "DEGRADED" else "DEGRADED"
            if (
                self._existing_outbox(degradation_dedupe_key(subject, opposite, str(sequence)))
                is not None
            ):
                marker_lost = True
                sequence += 1
            else:
                return None
        elif state == "RECOVERED" and previous_state is None:
            # No marker: either a clean install (nothing to recover from) or a
            # lost DEGRADED marker. Only a queued DEGRADED means an outage
            # actually happened, so the all-clear is not silently dropped.
            if self._existing_outbox(degradation_dedupe_key(subject, "DEGRADED", "1")) is not None:
                marker_lost = True
            else:
                return None
        elif (
            state == "DEGRADED"
            and previous_state is None
            and self._existing_outbox(degradation_dedupe_key(subject, "RECOVERED", "1")) is not None
        ):
            # Lost DEGRADED marker: a queued RECOVERED means the previous episode
            # ended and this is a genuinely new outage, not a first observation.
            marker_lost = True
            sequence += 1
        if marker_lost:
            LOGGER.warning(
                "telegram_degradation_marker_lost",
                subject=subject,
                marker_state=previous_state,
                observed_state=state,
                sequence=sequence,
            )
        record = self.enqueue_event(
            build_degradation_event(
                subject=subject,
                state=state,
                reason_code=reason_code,
                occurred_at=occurred_at,
                # The builder's key is subject+state+episode, so the sequence
                # alone is the durable episode token; the marker stores the
                # state alongside it for parsing.
                episode=str(sequence),
            )
        )
        if record is None:
            return None
        try:
            self._store.set_system_state(
                marker, f"{state}:{sequence}", updated_at=utc_timestamp(self._clock.now())
            )
        except Exception as exc:  # noqa: BLE001 - the outbox key still dedupes
            # The marker stays at the previous episode while this transition is
            # already durable in the outbox. It heals on the next transition for
            # the subject: a same-state retry recomputes the same outbox key and
            # dedupes while rewriting the marker, and an opposite-state
            # transition is recognized as a lost marker by probing the outbox
            # for the queued episode (above). So neither direction is dropped.
            LOGGER.warning(
                "telegram_degradation_state_unwritable",
                subject=subject,
                error_type=type(exc).__name__,
            )
        return record

    def enqueue_budget_threshold(
        self,
        *,
        period: str,
        period_key: str,
        spend_usd: str,
        threshold_usd: str,
        state: str,
        occurred_at: str,
    ) -> TelegramOutboxRecord | None:
        """Enqueue one budget-threshold alert derived from a durable audit event.

        Spend, threshold, state, and the period window are the audit draft's
        values; this method never re-sums the ledger. The unpriced count is a
        read-only count of the same window and degrades to ``None`` (``N/A``)
        when unreadable, never a fabricated zero. The event build and cooldown
        path live in the pure :func:`telegram_events.enqueue_budget_threshold`
        helper so the production method and unit tests share one implementation.
        Every failure is contained here so the budget path is never affected.
        """
        try:
            record = enqueue_budget_threshold(
                self,
                period=period,
                period_key=period_key,
                spend_usd=spend_usd,
                threshold_usd=threshold_usd,
                state=state,
                unpriced_count=self._unpriced_count(period, period_key),
                occurred_at=occurred_at,
            )
            return record if isinstance(record, TelegramOutboxRecord) else None
        except Exception as exc:  # noqa: BLE001 - a push never breaks the budget path
            LOGGER.warning(
                "telegram_budget_alert_failed",
                period=period,
                error_type=type(exc).__name__,
            )
            return None

    def _unpriced_count(self, period: str, period_key: str) -> int | None:
        """The honest unpriced row count for one window, or ``None`` if unreadable.

        Known residual: the ledger adapter returns ``()`` when the database file
        is absent, so that narrow case renders ``0`` rather than ``N/A``. A
        crossing implies a just-written ledger database, so this is effectively
        unreachable; every raised read error correctly renders ``N/A``. Fixing
        the distinction needs an adapter change, which is out of scope here.
        """
        try:
            rows = self._store.list_budget_records_for_period(period, period_key)
        except Exception as exc:  # noqa: BLE001 - N/A is honest, zero is not
            LOGGER.warning(
                "telegram_budget_unpriced_unreadable",
                period=period,
                error_type=type(exc).__name__,
            )
            return None
        return sum(1 for record in rows if record.unpriced)

    def notify_scan(self, scan_id: str) -> TelegramOutboxRecord | None:
        """Enqueue the one summary for a completed scan, recomputed from rows.

        Readiness is checked before any row or report read, so an unconfigured,
        untargetable, or fully toggled-off install pays no summary cost. The
        summary is rebuilt from persisted state on every call, so a resume,
        retry, or second completion computes the same dedupe key and the store's
        unique constraint makes the send single-shot. Every settings, store, and
        render failure is caught here, so this never raises into a scan path.
        """
        try:
            if not self._channel_ready():
                return None
            if self._target_chat() is None:
                return None
            if not self._push_enabled():
                return None
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
        """The one private-chat push target, or ``None`` when unsafe.

        Only positive (private) integer ids are eligible: a negative id is a
        group/supergroup, whose members would all see a push with no per-user
        authorization check. Exactly one eligible id is required; zero or
        several fail closed and record one counts-only warning.
        """
        allowlist = self._channel.allowlist()
        all_ids = tuple(allowlist.chat_ids)
        private_ids = tuple(
            chat_id
            for chat_id in all_ids
            if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0
        )
        if len(private_ids) == 1:
            return private_ids[0]
        LOGGER.warning(
            "telegram_push_target_unsafe",
            allowlist_count=len(all_ids),
            private_count=len(private_ids),
        )
        return None

    def _push_enabled(self) -> bool:
        """Whether either scan push toggle is on (a cheap pre-read gate)."""
        return self._read_toggle(_SCAN_REPORT_TOGGLE) or self._read_toggle(
            _SCAN_FAILED_ALERT_TOGGLE
        )

    def _event_enabled(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        # The toggle is checked before a row is built or written, so a disabled
        # event leaves no orphan outbox row.
        if event_type in (
            OPPORTUNITY_TRIGGERED_EVENT_TYPE,
            OPPORTUNITY_INVALIDATED_EVENT_TYPE,
            OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE,
        ):
            return self._read_toggle(_TRIGGER_ALERT_TOGGLE)
        if event_type == ENTRY_RECHECK_EVENT_TYPE:
            return self._read_toggle(_ENTRY_RECHECK_ALERT_TOGGLE)
        if event_type == POSITION_MANAGEMENT_EVENT_TYPE:
            return self._read_toggle(_POSITION_PUSH_TOGGLE)
        if event_type in (RISK_ALERT_EVENT_TYPE, DEGRADATION_EVENT_TYPE):
            return self._read_toggle(_RISK_PUSH_TOGGLE)
        if event_type == BUDGET_THRESHOLD_EVENT_TYPE:
            return self._read_toggle(_BUDGET_PUSH_TOGGLE)
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
            if toggle == _SCAN_FAILED_ALERT_TOGGLE:
                return bool(self._settings_service.read_failed_scan_alert())
            if toggle == _TRIGGER_ALERT_TOGGLE:
                return bool(self._settings_service.read_trigger_alert())
            if toggle == _ENTRY_RECHECK_ALERT_TOGGLE:
                return bool(self._settings_service.read_entry_recheck_alert())
            if toggle == _POSITION_PUSH_TOGGLE:
                return bool(self._settings_service.read_position_push())
            if toggle == _RISK_PUSH_TOGGLE:
                return bool(self._settings_service.read_risk_push())
            if toggle == _BUDGET_PUSH_TOGGLE:
                return bool(self._settings_service.read_budget_push())
            return False
        except Exception as exc:  # noqa: BLE001 - fail closed on an unreadable toggle
            LOGGER.warning(
                "telegram_notification_toggle_unreadable",
                toggle=toggle,
                error_type=type(exc).__name__,
            )
            return False

    def _existing_outbox(self, event_key: str) -> TelegramOutboxRecord | None | _OutboxProbeUnknown:
        """The stored row for ``event_key`` after a duplicate insert.

        ``None`` means the store reports no row. ``_OUTBOX_PROBE_UNKNOWN``
        means the read itself failed, which is not evidence of absence: the
        degradation heal treats it as a possible hit so the transition is
        enqueued (at-least-once) instead of silently dropped.
        """
        try:
            record = self._store.get_telegram_outbox(event_key)
        except Exception as exc:  # noqa: BLE001 - reported once, then degraded
            LOGGER.warning(
                "telegram_outbox_probe_unreadable",
                event_key=event_key,
                error_type=type(exc).__name__,
            )
            return _OUTBOX_PROBE_UNKNOWN
        return record if isinstance(record, TelegramOutboxRecord) else None

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
        cost_text = format_cost_total(cost)
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


def alert_cooldown_state_key(cooldown_key: str) -> str:
    """The persisted ``system_state`` key for one alert cooldown marker."""
    return f"{ALERT_COOLDOWN_STATE_PREFIX}{cooldown_key}"


def degradation_state_key(subject: str) -> str:
    """The persisted ``system_state`` key for one subject's last degradation state."""
    return f"{DEGRADATION_STATE_PREFIX}{subject}"


def parse_degradation_marker(value: str | None) -> tuple[str | None, int]:
    """Parse a persisted marker into ``(state, sequence)``.

    The current format is ``"STATE:sequence"``. Legacy plain ``"DEGRADED"`` or
    ``"RECOVERED"`` values parse with sequence 0 so an upgrade keeps its prior
    episode; any other value has no state and no sequence.
    """
    if not isinstance(value, str) or not value:
        return None, 0
    state, _, sequence_text = value.partition(":")
    if state not in DEGRADATION_STATES:
        return None, 0
    try:
        sequence = int(sequence_text) if sequence_text else 0
    except ValueError:
        sequence = 0
    return state, max(0, sequence)


class NotificationFreshnessSink:
    """Map a freshness transition to one DEGRADED/RECOVERED degradation push.

    ``FreshnessGuard`` calls this on a state transition and once on the first
    observation of a stream after startup (so a recovery that happened while
    the daemon was down still clears a stale DEGRADED marker). The service
    persists state plus an advancing episode sequence per subject, so a restart
    mid-episode re-observing STALE does not re-emit DEGRADED, and a recovery
    still emits exactly once.
    """

    def __init__(self, notifications: NotificationService) -> None:
        self._notifications = notifications

    def record_transition(
        self, *, stream: str, state: str, occurred_at: str, reason: str | None
    ) -> TelegramOutboxRecord | None:
        reason_code = "MARKET_DATA_STALE" if state == "DEGRADED" else "MARKET_DATA_RECOVERED"
        return self._notifications.enqueue_degradation_transition(
            subject=stream,
            state=state,
            reason_code=reason_code,
            occurred_at=occurred_at,
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


def _new_opportunities(store: StorePort, report_ids: Sequence[str]) -> int:
    """Count distinct opportunity ids across the scan's reports in one read.

    ``list_report_llm_json`` reads only the ``llm_json`` column for the given
    ids (chunked and parameterized), so this is one bounded batch read instead
    of N full ``get_report`` queries. Malformed entries are skipped, exactly as
    before; a store failure is contained by :meth:`notify_scan`.
    """
    if not report_ids:
        return 0
    seen: set[str] = set()
    for raw in store.list_report_llm_json(tuple(report_ids)):
        if not isinstance(raw, str) or not raw:
            continue
        try:
            value = json.loads(raw)
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
    "ALERT_COOLDOWN_STATE_PREFIX",
    "DEGRADATION_STATE_PREFIX",
    "DELIVERED_REASON",
    "DELIVERY_FAILED_REASON",
    "DELIVERY_PENDING_REASON",
    "NotificationChannel",
    "NotificationFreshnessSink",
    "NotificationService",
    "NotificationSettings",
    "TELEGRAM_NOT_CONFIGURED_REASON",
    "alert_cooldown_state_key",
    "degradation_state_key",
    "delivery_contract",
    "not_configured_delivery",
    "parse_degradation_marker",
]
