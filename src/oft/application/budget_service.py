"""Per-call budget accounting with day/week/month states (ADR-0005, D6).

Thresholds alert (``OK`` -> ``WARN`` -> ``EXCEEDED``) and are recorded locally;
they never block a call and never raise on ``EXCEEDED``. There is no outbound
notification here (Telegram is Milestone 5).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import structlog

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

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "daily_usd": "10.00",
    "weekly_usd": "50.00",
    "monthly_usd": "150.00",
    "warn_pct": 80.0,
}
_STATE_RANK = {"OK": 0, "WARN": 1, "EXCEEDED": 2}
_QUANTUM = Decimal("0.00000001")
_PERIODS = ("day", "week", "month")
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


class BudgetConfigError(Exception):
    """An operator threshold/period is invalid or pricing overrides are inconsistent."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _decimal(value: str, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BudgetConfigError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite():
        raise BudgetConfigError(f"{field} must be a finite decimal")
    return parsed


def _positive_decimal(value: str, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed <= 0:
        raise BudgetConfigError(f"{field} must be positive")
    return parsed


def _text(value: Decimal) -> str:
    return format(value, "f")


def _money(value: Decimal) -> str:
    return format(value.quantize(_QUANTUM, rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True)
class BudgetThresholds:
    daily_usd: Decimal
    weekly_usd: Decimal
    monthly_usd: Decimal
    warn_pct: float

    def threshold_for(self, period: str) -> Decimal:
        return {
            "day": self.daily_usd,
            "week": self.weekly_usd,
            "month": self.monthly_usd,
        }[period]

    def to_contract(self) -> dict[str, object]:
        return {
            "daily_usd": _text(self.daily_usd),
            "weekly_usd": _text(self.weekly_usd),
            "monthly_usd": _text(self.monthly_usd),
            "warn_pct": self.warn_pct,
        }


def _state_for(spend: Decimal, threshold: Decimal, warn_pct: float) -> str:
    if spend >= threshold:
        return "EXCEEDED"
    if spend >= threshold * Decimal(str(warn_pct)) / Decimal("100"):
        return "WARN"
    return "OK"


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
    ) -> None:
        self._store = store
        self._settings = settings
        # Loaded lazily on the first priced call (M11): a corrupt operator price
        # table degrades one scan instead of escaping a service constructor.
        self._price_table = price_table

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
        defaults = self._default_thresholds()
        stored = self._store.get_budget_settings()
        if stored is None:
            return defaults
        return BudgetThresholds(
            daily_usd=Decimal(stored.daily_usd),
            weekly_usd=Decimal(stored.weekly_usd),
            monthly_usd=Decimal(stored.monthly_usd),
            warn_pct=stored.warn_pct,
        )

    def set_thresholds(
        self,
        *,
        daily_usd: str | None = None,
        weekly_usd: str | None = None,
        monthly_usd: str | None = None,
        warn_pct: float | None = None,
        updated_at: str,
    ) -> BudgetThresholds:
        current = self.thresholds()
        candidate = BudgetThresholds(
            daily_usd=(
                _positive_decimal(daily_usd, field="daily_usd")
                if daily_usd is not None
                else current.daily_usd
            ),
            weekly_usd=(
                _positive_decimal(weekly_usd, field="weekly_usd")
                if weekly_usd is not None
                else current.weekly_usd
            ),
            monthly_usd=(
                _positive_decimal(monthly_usd, field="monthly_usd")
                if monthly_usd is not None
                else current.monthly_usd
            ),
            warn_pct=current.warn_pct if warn_pct is None else float(warn_pct),
        )
        if not 0 < candidate.warn_pct <= 100:
            raise BudgetConfigError("warn_pct must be greater than 0 and at most 100")
        self._store.save_budget_settings(
            BudgetSettingsRecord(
                daily_usd=_text(candidate.daily_usd),
                weekly_usd=_text(candidate.weekly_usd),
                monthly_usd=_text(candidate.monthly_usd),
                warn_pct=candidate.warn_pct,
                updated_at=updated_at,
            )
        )
        self._store.append_audit_event(
            AuditEventDraft(
                event_type="BUDGET_THRESHOLDS_UPDATED",
                actor={"kind": "USER", "id": "cli"},
                subject={"kind": "BUDGET_SETTINGS", "id": "default"},
                payload={"before": current.to_contract(), "after": candidate.to_contract()},
                created_at=updated_at,
            )
        )
        return candidate

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
        cost = _decimal(cost_usd, field="cost_usd")
        if unpriced is True:
            if cost != 0:
                raise BudgetConfigError("an unpriced call must record a zero cost")
        else:
            if cost <= 0:
                raise BudgetConfigError("a priced call must record a positive cost")
        return (
            PricedCall(
                unpriced=bool(unpriced),
                cost_usd=_money(cost) if not unpriced else "0",
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
            name: _state_for(
                prior_spends[name], thresholds.threshold_for(name), thresholds.warn_pct
            )
            for name in _PERIODS
        }
        states = {
            name: _state_for(spends[name], thresholds.threshold_for(name), thresholds.warn_pct)
            for name in _PERIODS
        }
        crossing = [
            name for name in _PERIODS if _STATE_RANK[states[name]] > _STATE_RANK[prior_states[name]]
        ]
        state = max(states.values(), key=lambda value: _STATE_RANK[value])
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
            if inserted:
                if not result.usage_reported:
                    self._emit_usage_missing(record=record, occurred_at=occurred_at)
                if invalid_table:
                    self._emit_price_table_invalid(record=record, occurred_at=occurred_at)
                if crossing:
                    self._emit_threshold_audit(
                        record=record,
                        states=states,
                        spends=spends,
                        thresholds=thresholds,
                        occurred_at=occurred_at,
                        crossing=crossing,
                    )
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
            return budget_state, audits

        stored_call, stored_budget = self._store.save_call_and_budget(call, draft, compute)
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
            "spend_usd": _money(spends[period]),
            "threshold_usd": _text(thresholds.threshold_for(period)),
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
    ) -> None:
        for draft in self._threshold_drafts(
            record=record,
            states=states,
            spends=spends,
            thresholds=thresholds,
            occurred_at=occurred_at,
            crossing=crossing,
        ):
            self._store.append_audit_event(draft)
            LOGGER.warning("budget_threshold", **draft.payload)

    # -- rollups ---------------------------------------------------------

    def usage(self, period: str, *, at: str) -> dict[str, object]:
        if period not in _PERIODS:
            raise BudgetConfigError("period must be day, week, or month")
        period_key = budget_period(at)[period]
        records = list(self._store.list_budget_records_for_period(period, period_key))
        threshold = self.thresholds()
        spend = sum((Decimal(record.cost_usd) for record in records), Decimal("0"))
        state = _state_for(spend, threshold.threshold_for(period), threshold.warn_pct)
        return {
            "period": period,
            "period_key": period_key,
            "spend_usd": _money(spend),
            "call_count": len(records),
            "unpriced_count": sum(1 for record in records if record.unpriced),
            "thresholds": threshold.to_contract(),
            "budget_state": state,
            "by_provider": _breakdown(records, "provider"),
            "by_model": _breakdown(records, "model"),
        }


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
            "spend_usd": _money(spend),
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
    "BudgetConfigError",
    "BudgetService",
    "BudgetThresholds",
    "DEFAULT_THRESHOLDS",
]
