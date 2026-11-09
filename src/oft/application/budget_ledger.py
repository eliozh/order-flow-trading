"""Public budget-ledger entry point; the logic lives in ``BudgetService`` (D6).

``record_call`` keeps its keyword signature. With no explicit price arguments it
computes unit prices and cost from the configured ``PriceTable``; an empty table
records the call honestly as ``unpriced`` with a zero cost.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from oft.application.budget_service import (
    DEFAULT_THRESHOLDS,
    BudgetConfigError,
    BudgetEntry,
    BudgetService,
    BudgetThresholds,
    budget_period,
)
from oft.config import Settings
from oft.domain.pricing import PriceTable
from oft.ports.llm import LLMResult, LLMUsage
from oft.ports.store import LLMCallRecord, StorePort


class BudgetLedger:
    """Compatibility facade over :class:`BudgetService` for existing callers."""

    def __init__(
        self,
        *,
        store: StorePort,
        settings: Settings | None = None,
        price_table: PriceTable | None = None,
    ) -> None:
        self._service = BudgetService(store=store, settings=settings, price_table=price_table)

    @property
    def service(self) -> BudgetService:
        return self._service

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
        return self._service.record_call(
            call_id=call_id,
            decision_id=decision_id,
            role=role,
            result=result,
            occurred_at=occurred_at,
            unpriced=unpriced,
            cost_usd=cost_usd,
            price_version=price_version,
            usage=usage,
        )

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
        return self._service.record_billed_call(
            call=call,
            result=result,
            occurred_at=occurred_at,
            unpriced=unpriced,
            cost_usd=cost_usd,
            price_version=price_version,
            usage=usage,
        )

    def thresholds(self) -> BudgetThresholds:
        return self._service.thresholds()

    def set_thresholds(
        self,
        *,
        daily_usd: str | None = None,
        weekly_usd: str | None = None,
        monthly_usd: str | None = None,
        warn_pct: float | None = None,
        updated_at: str,
    ) -> BudgetThresholds:
        return self._service.set_thresholds(
            daily_usd=daily_usd,
            weekly_usd=weekly_usd,
            monthly_usd=monthly_usd,
            warn_pct=warn_pct,
            updated_at=updated_at,
        )

    def usage(self, period: str, *, at: str) -> dict[str, object]:
        return self._service.usage(period, at=at)


def stable_call_id(decision_id: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{decision_id}:{attempt}".encode()).hexdigest()[:16].upper()
    return f"CALL-{digest}"


def budget_schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "contracts" / "budget-record.schema.json"


__all__ = [
    "BudgetConfigError",
    "BudgetEntry",
    "BudgetLedger",
    "BudgetThresholds",
    "DEFAULT_THRESHOLDS",
    "budget_period",
    "budget_schema_path",
    "stable_call_id",
]
