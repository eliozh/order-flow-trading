"""Pure read-model helpers for the budget thresholds and usage (D6/F10e).

Month projection, last-alert parsing, threshold candidate/validation, the
budget-state rule, and the money/text formatters live here so
``budget_service`` stays focused on the ledger and settings write paths. Every
function is pure: none of them touch the store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from oft.ports.store import AuditEventRecord, BudgetSettingsRecord

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "daily_usd": "10.00",
    "weekly_usd": "50.00",
    "monthly_usd": "150.00",
    "warn_pct": 80.0,
}
MONEY_FIELDS = ("daily_usd", "weekly_usd", "monthly_usd")
PERIODS = ("day", "week", "month")
STATE_RANK = {"OK": 0, "WARN": 1, "EXCEEDED": 2}

# The narrow set of read failures that degrade the usage block instead of
# failing the whole budget read. Kept in the application layer so the API
# boundary never imports sqlite3 directly.
BUDGET_READ_ERRORS = (sqlite3.Error, OSError, InvalidOperation)

QUANTUM = Decimal("0.00000001")


class BudgetConfigError(Exception):
    """An operator threshold/period is invalid or pricing overrides are inconsistent."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def parse_decimal(value: str, *, field: str) -> Decimal:
    """Parse one finite decimal string or raise a field-scoped error."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BudgetConfigError(
            f"{field} must be a decimal string",
            details=[
                {
                    "field": field,
                    "code": "invalid_decimal",
                    "message": f"{field} must be a decimal string",
                }
            ],
        ) from exc
    if not parsed.is_finite():
        raise BudgetConfigError(
            f"{field} must be a finite decimal",
            details=[
                {
                    "field": field,
                    "code": "invalid_decimal",
                    "message": f"{field} must be a finite decimal",
                }
            ],
        )
    return parsed


def parse_positive_decimal(value: str, *, field: str) -> Decimal:
    parsed = parse_decimal(value, field=field)
    if parsed <= 0:
        message = f"{field} must be positive"
        raise BudgetConfigError(
            message,
            details=[{"field": field, "code": "not_positive", "message": message}],
        )
    return parsed


def format_text(value: Decimal) -> str:
    return format(value, "f")


def format_money(value: Decimal) -> str:
    return format(value.quantize(QUANTUM, rounding=ROUND_HALF_UP), "f")


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
            "daily_usd": format_text(self.daily_usd),
            "weekly_usd": format_text(self.weekly_usd),
            "monthly_usd": format_text(self.monthly_usd),
            "warn_pct": self.warn_pct,
        }


def thresholds_from_record(
    stored: BudgetSettingsRecord | None, defaults: BudgetThresholds
) -> BudgetThresholds:
    """The effective thresholds: a stored row when present, else the defaults."""
    if stored is None:
        return defaults
    return BudgetThresholds(
        daily_usd=Decimal(stored.daily_usd),
        weekly_usd=Decimal(stored.weekly_usd),
        monthly_usd=Decimal(stored.monthly_usd),
        warn_pct=stored.warn_pct,
    )


def _collect_positive_decimal(
    raw: str, *, field: str, details: list[dict[str, Any]]
) -> Decimal | None:
    try:
        return parse_positive_decimal(raw, field=field)
    except BudgetConfigError as exc:
        details.extend(exc.details or [])
        return None


def candidate_thresholds(
    current: BudgetThresholds,
    *,
    daily_usd: str | None,
    weekly_usd: str | None,
    monthly_usd: str | None,
    warn_pct: float | None,
    enforce_ordering: bool = True,
) -> BudgetThresholds:
    """Build and validate a partial candidate, collecting one detail per field.

    ``enforce_ordering`` is true for the Web update path, where the form must
    report which lower bound is below its predecessor. The CLI setter keeps its
    historical permissiveness: the ledger tests use deliberately crossed
    thresholds to prove that each period alerts against its own bound.
    """
    details: list[dict[str, Any]] = []
    parsed: dict[str, Decimal] = {}
    for field, raw, fallback in (
        ("daily_usd", daily_usd, current.daily_usd),
        ("weekly_usd", weekly_usd, current.weekly_usd),
        ("monthly_usd", monthly_usd, current.monthly_usd),
    ):
        if raw is None:
            parsed[field] = fallback
            continue
        value = _collect_positive_decimal(raw, field=field, details=details)
        parsed[field] = fallback if value is None else value
    candidate = BudgetThresholds(
        daily_usd=parsed["daily_usd"],
        weekly_usd=parsed["weekly_usd"],
        monthly_usd=parsed["monthly_usd"],
        warn_pct=current.warn_pct if warn_pct is None else float(warn_pct),
    )
    _collect_range_and_ordering(candidate, enforce_ordering=enforce_ordering, details=details)
    if details:
        messages = "; ".join(str(detail["message"]) for detail in details)
        raise BudgetConfigError(messages, details=details)
    return candidate


def _collect_range_and_ordering(
    candidate: BudgetThresholds,
    *,
    enforce_ordering: bool,
    details: list[dict[str, Any]],
) -> None:
    if not 0 < candidate.warn_pct <= 100:
        details.append(
            {
                "field": "warn_pct",
                "code": "out_of_range",
                "message": "warn_pct must be greater than 0 and at most 100",
            }
        )
    # A malformed money field keeps its previous value as a placeholder; a
    # derived ordering error on top of that would mislead the operator.
    if any(detail["field"] in MONEY_FIELDS for detail in details):
        return
    if enforce_ordering and candidate.weekly_usd < candidate.daily_usd:
        details.append(
            {
                "field": "weekly_usd",
                "code": "ordering",
                "message": "weekly_usd must be greater than or equal to daily_usd",
            }
        )
    if enforce_ordering and candidate.monthly_usd < candidate.weekly_usd:
        details.append(
            {
                "field": "monthly_usd",
                "code": "ordering",
                "message": "monthly_usd must be greater than or equal to weekly_usd",
            }
        )


def state_for(spend: Decimal, threshold: Decimal, warn_pct: float) -> str:
    """Exact Decimal alert rule shared by the ledger, usage, and snapshots."""
    if spend >= threshold:
        return "EXCEEDED"
    if spend >= threshold * Decimal(str(warn_pct)) / Decimal("100"):
        return "WARN"
    return "OK"


def max_state(values: Iterable[object]) -> str:
    return max((str(value) for value in values), key=lambda value: STATE_RANK[value])


def utc_instant(instant: str) -> datetime:
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def month_bounds(instant: str) -> tuple[datetime, datetime]:
    """The UTC half-open ``[start, end)`` bounds of the month containing ``instant``."""
    parsed = utc_instant(instant)
    start = datetime(parsed.year, parsed.month, 1, tzinfo=UTC)
    end = datetime(
        parsed.year + (1 if parsed.month == 12 else 0),
        1 if parsed.month == 12 else parsed.month + 1,
        1,
        tzinfo=UTC,
    )
    return start, end


def projection_for(at: str, spend: Decimal) -> dict[str, object]:
    """Project month-end spend from month-to-date spend and elapsed time.

    The elapsed fraction is clamped to at least one second so a timestamp at
    the very start of the month cannot divide by zero. Zero spend always
    projects to zero; the math never fabricates a cost.
    """
    parsed = utc_instant(at)
    start, end = month_bounds(at)
    total_seconds = Decimal(str((end - start).total_seconds()))
    elapsed_seconds = max(Decimal(str((parsed - start).total_seconds())), Decimal("1"))
    fraction = elapsed_seconds / total_seconds
    projected = Decimal("0") if spend == 0 else spend / fraction
    return {
        "projected_month_end_usd": format_money(projected),
        "projection_basis": {
            "month_spend_usd": format_money(spend),
            "elapsed_fraction": float(fraction),
            "as_of": at,
        },
    }


def _finite_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def parse_last_alert(event: AuditEventRecord | None) -> dict[str, object] | None:
    """The newest ``BUDGET_THRESHOLD`` crossing, or ``None``.

    A malformed, incomplete, or non-numeric payload is reported as ``None``
    rather than escaping the read path: the usage screen still renders and
    thresholds stay editable.
    """
    if event is None:
        return None
    try:
        body = json.loads(event.payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return None
    period = payload.get("period")
    state = payload.get("state")
    provider = payload.get("provider")
    model = payload.get("model")
    spend = _finite_decimal(payload.get("spend_usd"))
    threshold = _finite_decimal(payload.get("threshold_usd"))
    if (
        period not in PERIODS
        or state not in ("WARN", "EXCEEDED")
        or spend is None
        or threshold is None
        or not isinstance(provider, str)
        or not isinstance(model, str)
    ):
        return None
    return {
        "period": period,
        "state": state,
        "spend_usd": format_text(spend),
        "threshold_usd": format_text(threshold),
        "occurred_at": event.created_at,
        "provider": provider,
        "model": model,
    }


__all__ = [
    "BUDGET_READ_ERRORS",
    "BudgetConfigError",
    "BudgetThresholds",
    "DEFAULT_THRESHOLDS",
    "MONEY_FIELDS",
    "PERIODS",
    "QUANTUM",
    "STATE_RANK",
    "candidate_thresholds",
    "format_money",
    "format_text",
    "max_state",
    "month_bounds",
    "parse_decimal",
    "parse_last_alert",
    "parse_positive_decimal",
    "projection_for",
    "state_for",
    "thresholds_from_record",
    "utc_instant",
]
