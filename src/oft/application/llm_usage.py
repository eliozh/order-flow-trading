"""Shared LLM usage/cost aggregation for decision provenance (ADR-0005).

The analysis, entry, and management roles all record the same provenance
``usage`` map and ``cost`` block from their call and budget rows. This module is
the single implementation; it accepts any sequence so a caller's tuple/list
choice cannot change the totals.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from oft.application.budget_ledger import BudgetEntry
from oft.ports.llm import LLMUsage
from oft.ports.store import LLMCallRecord

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def total_usage(calls: Sequence[LLMCallRecord]) -> dict[str, int]:
    """Sum usage across every call row of a decision (including repairs)."""
    totals = dict.fromkeys(_USAGE_FIELDS, 0)
    for call in calls:
        try:
            payload = json.loads(call.usage_json or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in _USAGE_FIELDS:
            value = payload.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field] += value
    return LLMUsage(**totals).to_contract()


def total_cost(budgets: Sequence[BudgetEntry]) -> tuple[str, str, bool]:
    """Sum cost across every budget row of a decision (including repairs)."""
    if not budgets:
        return "0", "stub/v1", True
    amount = Decimal("0")
    price_version = "stub/v1"
    unpriced = False
    for entry in budgets:
        payload = entry.payload
        try:
            amount += Decimal(str(payload.get("cost_usd", "0")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        price_version = str(payload.get("price_version") or price_version)
        unpriced = unpriced or bool(payload.get("unpriced", True))
    return format(amount, "f"), price_version, unpriced


__all__ = ["total_cost", "total_usage"]
