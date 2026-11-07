"""One ``budget-record`` row per LLM call (no rollups in this slice).

The stub provider has no configured price, so its rows are ``unpriced=true``
and the amount is zero. They are still written and surfaced; a call is never
silently treated as free (ADR-0005).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from oft.ports.llm import LLMResult, LLMUsage
from oft.ports.store import BudgetRecord, StorePort

DEFAULT_THRESHOLDS = {
    "daily_usd": "10.00",
    "weekly_usd": "50.00",
    "monthly_usd": "150.00",
    "warn_pct": 80.0,
}

_STUB_PRICE_VERSION = "stub/v1"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def budget_period(instant: str) -> dict[str, str]:
    parsed = _parse(instant)
    iso_year, iso_week, _ = parsed.isocalendar()
    return {
        "day": parsed.strftime("%Y-%m-%d"),
        "week": f"{iso_year}-W{iso_week:02d}",
        "month": parsed.strftime("%Y-%m"),
    }


@dataclass(frozen=True)
class BudgetEntry:
    record: BudgetRecord
    payload: dict[str, Any]


def _record_id(call_id: str) -> str:
    return f"BUD-{hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:16].upper()}"


class BudgetLedger:
    def __init__(self, *, store: StorePort) -> None:
        self._store = store

    def record_call(
        self,
        *,
        call_id: str,
        decision_id: str | None,
        role: str,
        result: LLMResult,
        occurred_at: str,
        unpriced: bool = True,
        cost_usd: str = "0",
        price_version: str = _STUB_PRICE_VERSION,
        usage: LLMUsage | None = None,
    ) -> BudgetEntry:
        resolved_usage = usage or result.usage
        period = budget_period(occurred_at)
        record_id = _record_id(call_id)
        payload: dict[str, Any] = {
            "schema_version": "1.0.0",
            "record_id": record_id,
            "call_id": call_id,
            "decision_id": decision_id,
            "occurred_at": occurred_at,
            "provider": result.provider,
            "model": result.model,
            "usage": resolved_usage.to_contract(),
            "unit_prices": {
                "input_per_mtok": "0",
                "output_per_mtok": "0",
                "cache_read_per_mtok": "0",
                "cache_write_per_mtok": "0",
                "currency": "USD",
            },
            "cost_usd": cost_usd,
            "price_version": price_version,
            "unpriced": unpriced,
            "period": period,
            "thresholds": dict(DEFAULT_THRESHOLDS),
            "budget_state": "OK",
        }
        record = BudgetRecord(
            record_id=record_id,
            call_id=call_id,
            decision_id=decision_id,
            occurred_at=occurred_at,
            provider=result.provider,
            model=result.model,
            cost_usd=cost_usd,
            price_version=price_version,
            unpriced=unpriced,
            budget_state="OK",
            period_day=period["day"],
            payload_json=json.dumps(payload, sort_keys=True),
        )
        self._store.save_budget_record(record)
        return BudgetEntry(record=record, payload=payload)


def stable_call_id(decision_id: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{decision_id}:{attempt}".encode()).hexdigest()[:16].upper()
    return f"CALL-{digest}"


def budget_schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "contracts" / "budget-record.schema.json"
