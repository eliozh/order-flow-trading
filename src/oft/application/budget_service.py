"""Per-call budget accounting with day/week/month states (ADR-0005, D6).

Thresholds alert (``OK`` -> ``WARN`` -> ``EXCEEDED``) and are recorded locally;
they never block a call and never raise on ``EXCEEDED``. A crossing additionally
enqueues one read-only Telegram push derived from the same durable
``BUDGET_THRESHOLD`` audit draft, after the ledger row (and, on the billed path,
the call) transaction has committed. The push is best-effort and alert-only: it
never changes a ledger row, a threshold, or the call outcome. Delivery is
at-most-once - a crash between the commit and the outbox enqueue loses that
crossing's alert, because there is no reconciliation pass from the durable audit
rows.

Documented maintainability exception: this module sits just over the 800-line
soft ceiling because it owns the whole budget write model - thresholds, the
per-call ledger, the durable threshold-audit drafts, and their best-effort alert
hand-off. The alert wiring stays next to the drafts it must forward verbatim (so
there is no second spend calculation); extracting it would split that invariant
across modules. Deferred: move the threshold-draft and alert hand-off into a
dedicated ``budget_alerts`` module when this write model next changes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog

from oft.application.budget_read_model import (
    DEFAULT_THRESHOLDS,
    PERIODS,
    STATE_RANK,
    BudgetConfigError,
    BudgetThresholds,
    candidate_thresholds,
    format_money,
    format_text,
    max_state,
    parse_decimal,
    parse_last_alert,
    projection_for,
    state_for,
    thresholds_from_record,
)
from oft.application.telegram_events import BudgetAlertSink
from oft.config import Settings
from oft.domain.budget_periods import budget_record_id, period_parts
from oft.domain.pricing import (
    EMPTY_PRICE_VERSION,
    PricedCall,
    PriceTable,
    PriceTableError,
    TokenUsage,
)
from oft.ports.llm import LLMResult, LLMUsage, redact
from oft.ports.store import (
    AuditEventDraft,
    BudgetRecord,
    BudgetSettingsRecord,
    LLMCallRecord,
    StorePort,
)

LOGGER = structlog.get_logger(__name__)

# Serializes the read-compute-write cycle of the legacy ``record_call`` path
# across every BudgetService instance in the process. The billed path
# (``record_billed_call``) is serialized by one SQLite ``BEGIN IMMEDIATE``
# transaction instead, so it is also safe across processes (M2/F4).
_LEDGER_LOCK = threading.Lock()

_PERIODS = PERIODS
_SECRET_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "api_token",
)


def budget_period(instant: str) -> dict[str, str]:
    day, week, month = period_parts(instant)
    return {"day": day, "week": week, "month": month}


@dataclass(frozen=True)
class BudgetEntry:
    record: BudgetRecord
    payload: dict[str, Any]


def _usage_tokens(usage: LLMUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )


class BudgetService:
    """The single writer of ``budget_ledger`` rows and threshold state."""

    def __init__(
        self,
        *,
        store: StorePort,
        settings: Settings | None = None,
        price_table: PriceTable | None = None,
        notifications: BudgetAlertSink | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        # Loaded lazily on the first priced call (M11): a corrupt operator price
        # table degrades one scan instead of escaping a service constructor.
        self._price_table = price_table
        # Best-effort threshold push; ``None`` keeps the ledger fully local.
        self._notifications = notifications

    def _table(self) -> PriceTable:
        if self._price_table is None:
            if self._settings is not None:
                self._price_table = self._settings.price_table()
            else:
                self._price_table = PriceTable(version=EMPTY_PRICE_VERSION, models={})
        return self._price_table

    # -- thresholds ------------------------------------------------------

    def _default_thresholds(self) -> BudgetThresholds:
        if self._settings is None:
            return BudgetThresholds(
                daily_usd=Decimal(DEFAULT_THRESHOLDS["daily_usd"]),
                weekly_usd=Decimal(DEFAULT_THRESHOLDS["weekly_usd"]),
                monthly_usd=Decimal(DEFAULT_THRESHOLDS["monthly_usd"]),
                warn_pct=float(DEFAULT_THRESHOLDS["warn_pct"]),
            )
        return BudgetThresholds(
            daily_usd=Decimal(self._settings.budget_daily_usd),
            weekly_usd=Decimal(self._settings.budget_weekly_usd),
            monthly_usd=Decimal(self._settings.budget_monthly_usd),
            warn_pct=self._settings.budget_warn_pct,
        )

    def thresholds(self) -> BudgetThresholds:
        return thresholds_from_record(self._store.get_budget_settings(), self._default_thresholds())

    def _settings_record(
        self, candidate: BudgetThresholds, *, updated_at: str
    ) -> BudgetSettingsRecord:
        return BudgetSettingsRecord(
            daily_usd=format_text(candidate.daily_usd),
            weekly_usd=format_text(candidate.weekly_usd),
            monthly_usd=format_text(candidate.monthly_usd),
            warn_pct=candidate.warn_pct,
            updated_at=updated_at,
        )

    def _thresholds_audit(
        self,
        *,
        current: BudgetThresholds,
        candidate: BudgetThresholds,
        updated_at: str,
        actor: str,
        reason: str,
    ) -> AuditEventDraft:
        payload: dict[str, Any] = {
            "before": current.to_contract(),
            "after": candidate.to_contract(),
        }
        if reason:
            payload["reason"] = reason
        return AuditEventDraft(
            event_type="BUDGET_THRESHOLDS_UPDATED",
            actor={"kind": "USER", "id": actor},
            subject={"kind": "BUDGET_SETTINGS", "id": "default"},
            payload=payload,
            created_at=updated_at,
        )

    def set_thresholds(
        self,
        *,
        daily_usd: str | None = None,
        weekly_usd: str | None = None,
        monthly_usd: str | None = None,
        warn_pct: float | None = None,
        updated_at: str,
        actor: str = "cli",
        reason: str = "",
    ) -> BudgetThresholds:
        captured: dict[str, BudgetThresholds] = {}

        def build(
            stored: BudgetSettingsRecord | None,
        ) -> tuple[BudgetSettingsRecord, AuditEventDraft]:
            current = thresholds_from_record(stored, self._default_thresholds())
            candidate = candidate_thresholds(
                current,
                daily_usd=daily_usd,
                weekly_usd=weekly_usd,
                monthly_usd=monthly_usd,
                warn_pct=warn_pct,
                enforce_ordering=False,
            )
            captured["thresholds"] = candidate
            return (
                self._settings_record(candidate, updated_at=updated_at),
                self._thresholds_audit(
                    current=current,
                    candidate=candidate,
                    updated_at=updated_at,
                    actor=actor,
                    reason=reason,
                ),
            )

        self._store.save_budget_settings_change(build)
        return captured["thresholds"]

    def update_thresholds(
        self,
        *,
        daily_usd: str | None = None,
        weekly_usd: str | None = None,
        monthly_usd: str | None = None,
        warn_pct: float | None = None,
        updated_at: str,
        actor: str = "web",
        reason: str = "",
    ) -> tuple[BudgetThresholds, bool]:
        """Compare-and-write atomically: an unchanged candidate stores nothing.

        The current row is read inside the store's ``BEGIN IMMEDIATE``
        transaction, and the typed :class:`BudgetThresholds` values are compared
        (``Decimal`` equality), so ``"10"`` against a stored ``"10.00"`` is a
        no-op rather than a spurious audit.
        """
        captured: dict[str, Any] = {}

        def build(
            stored: BudgetSettingsRecord | None,
        ) -> tuple[BudgetSettingsRecord, AuditEventDraft] | None:
            current = thresholds_from_record(stored, self._default_thresholds())
            candidate = candidate_thresholds(
                current,
                daily_usd=daily_usd,
                weekly_usd=weekly_usd,
                monthly_usd=monthly_usd,
                warn_pct=warn_pct,
            )
            if candidate == current:
                captured.update(thresholds=current, changed=False)
                return None
            captured.update(thresholds=candidate, changed=True)
            return (
                self._settings_record(candidate, updated_at=updated_at),
                self._thresholds_audit(
                    current=current,
                    candidate=candidate,
                    updated_at=updated_at,
                    actor=actor,
                    reason=reason,
                ),
            )

        self._store.save_budget_settings_change(build)
        return captured["thresholds"], captured["changed"]

    # -- per-call ledger -------------------------------------------------

    def _priced_call(
        self,
        *,
        result: LLMResult,
        usage: LLMUsage,
        unpriced: bool | None,
        cost_usd: str | None,
        price_version: str | None,
    ) -> tuple[PricedCall, bool]:
        """Return the priced call and whether the price table is unusable (F5)."""
        overrides = (unpriced, cost_usd, price_version)
        provided = [value is not None for value in overrides]
        if any(provided) and not all(provided):
            raise BudgetConfigError(
                "pricing overrides must supply unpriced, cost_usd, and price_version together"
            )
        if not all(provided):
            if not result.usage_reported:
                # The provider omitted or coerced its usage; never silently
                # record a priced zero (M10).
                return _empty_unpriced(), False
            try:
                table = self._table()
            except PriceTableError:
                # A corrupt operator table degrades accounting, not the call
                # (F5): record the billed call honestly as unpriced.
                return _empty_unpriced(), True
            return table.price_call(result.provider, result.model, _usage_tokens(usage)), False
        assert cost_usd is not None and price_version is not None
        cost = parse_decimal(cost_usd, field="cost_usd")
        if unpriced is True:
            if cost != 0:
                raise BudgetConfigError("an unpriced call must record a zero cost")
        else:
            if cost <= 0:
                raise BudgetConfigError("a priced call must record a positive cost")
        return (
            PricedCall(
                unpriced=bool(unpriced),
                cost_usd=format_money(cost) if not unpriced else "0",
                price_version=price_version,
                input_per_mtok="0",
                output_per_mtok="0",
                cache_read_per_mtok="0",
                cache_write_per_mtok="0",
            ),
            False,
        )

    def _states(
        self,
        prior: Mapping[str, Mapping[str, Any]],
        current_cost: Decimal,
        thresholds: BudgetThresholds,
    ) -> tuple[dict[str, str], dict[str, Decimal], list[str], str]:
        """Derive pre/post states from pre-call totals (F2/F3).

        Each period is compared against its own pre-call rank, so a rollover and
        a crossing masked by another period's global state both alert.
        """
        prior_spends = {name: Decimal(str(prior[name]["spend_usd"])) for name in _PERIODS}
        spends = {name: prior_spends[name] + current_cost for name in _PERIODS}
        prior_states = {
            name: state_for(prior_spends[name], thresholds.threshold_for(name), thresholds.warn_pct)
            for name in _PERIODS
        }
        states = {
            name: state_for(spends[name], thresholds.threshold_for(name), thresholds.warn_pct)
            for name in _PERIODS
        }
        crossing = [
            name for name in _PERIODS if STATE_RANK[states[name]] > STATE_RANK[prior_states[name]]
        ]
        state = max(states.values(), key=lambda value: STATE_RANK[value])
        return states, spends, crossing, state

    def _payload(
        self,
        *,
        record_id: str,
        call_id: str,
        decision_id: str | None,
        occurred_at: str,
        result: LLMResult,
        usage: LLMUsage,
        priced: PricedCall,
        period: Mapping[str, str],
        thresholds: BudgetThresholds,
        budget_state: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "record_id": record_id,
            "call_id": call_id,
            "decision_id": decision_id,
            "occurred_at": occurred_at,
            "provider": result.provider,
            "model": result.model,
            "usage": usage.to_contract(),
            "unit_prices": {
                "input_per_mtok": priced.input_per_mtok,
                "output_per_mtok": priced.output_per_mtok,
                "cache_read_per_mtok": priced.cache_read_per_mtok,
                "cache_write_per_mtok": priced.cache_write_per_mtok,
                "currency": "USD",
            },
            "cost_usd": priced.cost_usd,
            "price_version": priced.price_version,
            "unpriced": priced.unpriced,
            "period": dict(period),
            "thresholds": thresholds.to_contract(),
            "budget_state": budget_state,
        }

    def record_call(
        self,
        *,
        call_id: str,
        decision_id: str | None,
        role: str,
        result: LLMResult,
        occurred_at: str,
        unpriced: bool | None = None,
        cost_usd: str | None = None,
        price_version: str | None = None,
        usage: LLMUsage | None = None,
    ) -> BudgetEntry:
        resolved_usage = usage or result.usage
        with _LEDGER_LOCK:
            priced, invalid_table = self._priced_call(
                result=result,
                usage=resolved_usage,
                unpriced=unpriced,
                cost_usd=cost_usd,
                price_version=price_version,
            )
            period = budget_period(occurred_at)
            thresholds = self.thresholds()
            prior = self._store.budget_totals(
                day=period["day"], week=period["week"], month=period["month"]
            )
            states, spends, crossing, budget_state = self._states(
                prior, Decimal(priced.cost_usd), thresholds
            )
            record_id = budget_record_id(call_id)
            payload = self._payload(
                record_id=record_id,
                call_id=call_id,
                decision_id=decision_id,
                occurred_at=occurred_at,
                result=result,
                usage=resolved_usage,
                priced=priced,
                period=period,
                thresholds=thresholds,
                budget_state=budget_state,
            )
            record = BudgetRecord(
                record_id=record_id,
                call_id=call_id,
                decision_id=decision_id,
                occurred_at=occurred_at,
                provider=result.provider,
                model=result.model,
                cost_usd=priced.cost_usd,
                price_version=priced.price_version,
                unpriced=priced.unpriced,
                budget_state=budget_state,
                period_day=period["day"],
                payload_json=_json(payload),
                period_week=period["week"],
                period_month=period["month"],
                role=role,
            )
            inserted = self._store.save_budget_record(record)
            drafts: list[AuditEventDraft] = []
            if inserted:
                if not result.usage_reported:
                    self._emit_usage_missing(record=record, occurred_at=occurred_at)
                if invalid_table:
                    self._emit_price_table_invalid(record=record, occurred_at=occurred_at)
                if crossing:
                    drafts = self._emit_threshold_audit(
                        record=record,
                        states=states,
                        spends=spends,
                        thresholds=thresholds,
                        occurred_at=occurred_at,
                        crossing=crossing,
                    )
        # The ledger row and its audit are committed; notify outside the ledger
        # mutex so outbox/system_state writes cannot lengthen the hot-path lock.
        self._notify_budget_thresholds(drafts)
        return BudgetEntry(record=record, payload=payload)

    def record_billed_call(
        self,
        *,
        call: LLMCallRecord,
        result: LLMResult,
        occurred_at: str,
        unpriced: bool | None = None,
        cost_usd: str | None = None,
        price_version: str | None = None,
        usage: LLMUsage | None = None,
    ) -> tuple[LLMCallRecord, BudgetEntry]:
        """Write the call row, budget row, and threshold audit in one transaction (F4).

        The state is computed from totals read inside the transaction, so a
        concurrent or cross-process invocation cannot lose a real crossing.
        """
        resolved_usage = usage or result.usage
        priced, invalid_table = self._priced_call(
            result=result,
            usage=resolved_usage,
            unpriced=unpriced,
            cost_usd=cost_usd,
            price_version=price_version,
        )
        period = budget_period(occurred_at)
        thresholds = self.thresholds()
        current_cost = Decimal(priced.cost_usd)
        record_id = budget_record_id(call.call_id)
        payload = self._payload(
            record_id=record_id,
            call_id=call.call_id,
            decision_id=call.decision_id,
            occurred_at=occurred_at,
            result=result,
            usage=resolved_usage,
            priced=priced,
            period=period,
            thresholds=thresholds,
            budget_state="OK",
        )
        draft = BudgetRecord(
            record_id=record_id,
            call_id=call.call_id,
            decision_id=call.decision_id,
            occurred_at=occurred_at,
            provider=result.provider,
            model=result.model,
            cost_usd=priced.cost_usd,
            price_version=priced.price_version,
            unpriced=priced.unpriced,
            budget_state="OK",
            period_day=period["day"],
            payload_json=_json(payload),
            period_week=period["week"],
            period_month=period["month"],
            role=call.role,
        )

        captured_audits: list[AuditEventDraft] = []

        def compute(
            prior: dict[str, dict[str, Any]],
        ) -> tuple[str, list[AuditEventDraft]]:
            states, spends, crossing, budget_state = self._states(prior, current_cost, thresholds)
            audits = (
                self._threshold_drafts(
                    record=draft,
                    states=states,
                    spends=spends,
                    thresholds=thresholds,
                    occurred_at=occurred_at,
                    crossing=crossing,
                )
                if crossing
                else []
            )
            # Capture the exact drafts the transaction will commit so the push
            # runs only after the write succeeds and carries the same values.
            captured_audits[:] = audits
            return budget_state, audits

        stored_call, stored_budget = self._store.save_call_and_budget(call, draft, compute)
        # The transaction committed the call, ledger row, and audit; a push
        # failure from here on can no longer change any of them.
        self._notify_budget_thresholds(captured_audits)
        entry_payload = _loads_payload(stored_budget.payload_json, fallback=payload)
        if not result.usage_reported:
            self._emit_usage_missing(record=stored_budget, occurred_at=occurred_at)
        if invalid_table:
            self._emit_price_table_invalid(record=stored_budget, occurred_at=occurred_at)
        return stored_call, BudgetEntry(record=stored_budget, payload=entry_payload)

    def _emit_usage_missing(self, *, record: BudgetRecord, occurred_at: str) -> None:
        payload = {
            "provider": record.provider,
            "model": record.model,
            "call_id": record.call_id,
            "reason": "the provider response reported no usable usage object",
        }
        self._store.append_audit_event(
            AuditEventDraft(
                event_type="LLM_USAGE_MISSING",
                actor={"kind": "SYSTEM", "id": "budget_service"},
                subject={"kind": "BUDGET", "id": record.record_id},
                payload=payload,
                created_at=occurred_at,
            )
        )

    def _emit_price_table_invalid(self, *, record: BudgetRecord, occurred_at: str) -> None:
        """Surface a corrupt operator price table without failing the call (F5)."""
        reason = redact("the operator price table is unreadable or inconsistent", self._secrets())
        payload = {
            "provider": record.provider,
            "model": record.model,
            "call_id": record.call_id,
            "reason": reason,
        }
        self._store.append_audit_event(
            AuditEventDraft(
                event_type="LLM_PRICE_TABLE_INVALID",
                actor={"kind": "SYSTEM", "id": "budget_service"},
                subject={"kind": "BUDGET", "id": record.record_id},
                payload=payload,
                created_at=occurred_at,
            )
        )
        LOGGER.warning("llm_price_table_invalid", **payload)

    def _secrets(self) -> tuple[str, ...]:
        if self._settings is None:
            return ()
        values: list[str] = []
        for name in _SECRET_FIELDS:
            secret = getattr(self._settings, name, None)
            getter = getattr(secret, "get_secret_value", None)
            if callable(getter):
                value = getter()
                if isinstance(value, str) and value.strip():
                    values.append(value)
        return tuple(values)

    def _threshold_draft(
        self,
        *,
        record: BudgetRecord,
        states: Mapping[str, str],
        spends: Mapping[str, Decimal],
        thresholds: BudgetThresholds,
        occurred_at: str,
        period: str,
    ) -> AuditEventDraft:
        payload = {
            "period": period,
            "period_key": budget_period(record.occurred_at)[period],
            "spend_usd": format_money(spends[period]),
            "threshold_usd": format_text(thresholds.threshold_for(period)),
            "state": states[period],
            "provider": record.provider,
            "model": record.model,
        }
        return AuditEventDraft(
            event_type="BUDGET_THRESHOLD",
            actor={"kind": "SYSTEM", "id": "budget_service"},
            subject={"kind": "BUDGET", "id": record.record_id},
            payload=payload,
            created_at=occurred_at,
        )

    def _threshold_drafts(
        self,
        *,
        record: BudgetRecord,
        states: Mapping[str, str],
        spends: Mapping[str, Decimal],
        thresholds: BudgetThresholds,
        occurred_at: str,
        crossing: list[str],
    ) -> list[AuditEventDraft]:
        """One audit per crossed period; a call may cross several at once (S1)."""
        return [
            self._threshold_draft(
                record=record,
                states=states,
                spends=spends,
                thresholds=thresholds,
                occurred_at=occurred_at,
                period=period,
            )
            for period in crossing
        ]

    def _emit_threshold_audit(
        self,
        *,
        record: BudgetRecord,
        states: Mapping[str, str],
        spends: Mapping[str, Decimal],
        thresholds: BudgetThresholds,
        occurred_at: str,
        crossing: list[str],
    ) -> list[AuditEventDraft]:
        drafts = self._threshold_drafts(
            record=record,
            states=states,
            spends=spends,
            thresholds=thresholds,
            occurred_at=occurred_at,
            crossing=crossing,
        )
        for draft in drafts:
            self._store.append_audit_event(draft)
            LOGGER.warning("budget_threshold", **draft.payload)
        return drafts

    def _notify_budget_thresholds(self, drafts: Sequence[AuditEventDraft]) -> None:
        """Push one alert per durable threshold audit draft, best-effort.

        The payload values are the audit event's own (spend, threshold, state,
        and window); nothing is re-summed and no threshold is changed. Each
        draft is notified independently, so a failing sink for one period cannot
        skip the remaining periods; every failure is contained here. Delivery is
        at-most-once: the audit has already committed, and a crash before the
        outbox enqueue is not reconciled.
        """
        if self._notifications is None or not drafts:
            return
        for draft in drafts:
            payload = draft.payload
            try:
                self._notifications.enqueue_budget_threshold(
                    period=str(payload["period"]),
                    period_key=str(payload["period_key"]),
                    spend_usd=str(payload["spend_usd"]),
                    threshold_usd=str(payload["threshold_usd"]),
                    state=str(payload["state"]),
                    occurred_at=draft.created_at,
                )
            except Exception as exc:  # noqa: BLE001 - one draft never breaks the ledger
                LOGGER.warning(
                    "budget_threshold_alert_failed",
                    period=str(payload.get("period")),
                    state=str(payload.get("state")),
                    error_type=type(exc).__name__,
                )

    # -- rollups ---------------------------------------------------------

    def usage(
        self,
        period: str,
        *,
        at: str,
        thresholds: BudgetThresholds | None = None,
    ) -> dict[str, object]:
        if period not in _PERIODS:
            raise BudgetConfigError("period must be day, week, or month")
        period_key = budget_period(at)[period]
        records = list(self._store.list_budget_records_for_period(period, period_key))
        threshold = self.thresholds() if thresholds is None else thresholds
        spend = sum((Decimal(record.cost_usd) for record in records), Decimal("0"))
        state = state_for(spend, threshold.threshold_for(period), threshold.warn_pct)
        return {
            "period": period,
            "period_key": period_key,
            "spend_usd": format_money(spend),
            "call_count": len(records),
            "unpriced_count": sum(1 for record in records if record.unpriced),
            "thresholds": threshold.to_contract(),
            "budget_state": state,
            "by_provider": _breakdown(records, "provider"),
            "by_model": _breakdown(records, "model"),
        }

    def projection(self, at: str) -> dict[str, object]:
        """Project month-end spend; see :func:`projection_for` for the math."""
        month = self.usage("month", at=at)
        return projection_for(at, Decimal(str(month["spend_usd"])))

    def snapshot(self, at: str, *, thresholds: BudgetThresholds | None = None) -> dict[str, object]:
        """The single authoritative read model used by GET, PUT, and the CLI.

        The three usage windows are computed once and the month window feeds the
        projection, so one snapshot scans the ledger once per period and reads
        the thresholds once.
        """
        current = self.thresholds() if thresholds is None else thresholds
        windows = {period: self.usage(period, at=at, thresholds=current) for period in _PERIODS}
        month_spend = Decimal(str(windows["month"]["spend_usd"]))
        return {
            "thresholds": current.to_contract(),
            "usage_quality": "COMPLETE",
            "budget_state": max_state(window["budget_state"] for window in windows.values()),
            "periods": {
                period: {
                    "period_key": window["period_key"],
                    "spend_usd": window["spend_usd"],
                    "call_count": window["call_count"],
                    "unpriced_count": window["unpriced_count"],
                    "budget_state": window["budget_state"],
                }
                for period, window in windows.items()
            },
            **projection_for(at, month_spend),
            "last_alert": self.last_alert(),
        }

    def last_alert(self) -> dict[str, object] | None:
        """The newest well-formed ``BUDGET_THRESHOLD`` crossing, or ``None``."""
        return parse_last_alert(self._store.last_audit_event_of_type("BUDGET_THRESHOLD"))


def _breakdown(records: list[BudgetRecord], attribute: str) -> list[dict[str, object]]:
    grouped: dict[str, list[BudgetRecord]] = {}
    for record in records:
        grouped.setdefault(str(getattr(record, attribute)), []).append(record)
    rows: list[dict[str, object]] = []
    for key in sorted(grouped):
        items = grouped[key]
        spend = sum((Decimal(record.cost_usd) for record in items), Decimal("0"))
        row: dict[str, object] = {
            attribute: key,
            "spend_usd": format_money(spend),
            "call_count": len(items),
            "unpriced_count": sum(1 for record in items if record.unpriced),
        }
        rows.append(row)
    return rows


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True)


def _loads_payload(payload_json: str, *, fallback: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return dict(fallback)
    return payload if isinstance(payload, dict) else dict(fallback)


def _empty_unpriced() -> PricedCall:
    return PricedCall(
        unpriced=True,
        cost_usd="0",
        price_version=EMPTY_PRICE_VERSION,
        input_per_mtok="0",
        output_per_mtok="0",
        cache_read_per_mtok="0",
        cache_write_per_mtok="0",
    )


__all__ = [
    "BudgetAlertSink",
    "BudgetConfigError",
    "BudgetService",
    "BudgetThresholds",
    "DEFAULT_THRESHOLDS",
]
