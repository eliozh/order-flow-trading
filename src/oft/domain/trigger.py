"""Deterministic trigger evaluation: snapshot hash, condition results, no stale.

Only code evaluates conditions. ``PRICE_TOUCH_ZONE`` is the sole condition type
in this walking-skeleton slice; any other type is reported ``evaluable=false``
and can never be treated as met (ADR-0011).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

SUPPORTED_TYPES = frozenset({"PRICE_TOUCH_ZONE"})
DEFAULT_STALE_AFTER_SECONDS = 60


@dataclass(frozen=True)
class ConditionResult:
    condition_id: str
    type: str
    met: bool
    observed_value: str | None
    threshold: str | None
    evaluated_at: str
    data_quality: str
    evaluable: bool
    note: str

    def to_contract(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "type": self.type,
            "met": self.met,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "evaluated_at": self.evaluated_at,
            "data_quality": self.data_quality,
            "evaluable": self.evaluable,
            "note": self.note,
        }


@dataclass(frozen=True)
class TriggerEvaluation:
    met: bool
    stale: bool
    snapshot_id: str
    snapshot_hash: str
    as_of: str
    data_quality: str
    results: tuple[ConditionResult, ...]
    reasons: tuple[str, ...]


def snapshot_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def evaluate_condition(
    condition: Any,
    *,
    last_price: Decimal,
    evaluated_at: str,
    data_quality: str,
) -> ConditionResult:
    """Evaluate one condition against the frozen snapshot price."""
    params = condition.params
    threshold_value = None
    observed = str(last_price)
    if not condition.evaluable:
        return ConditionResult(
            condition_id=condition.condition_id,
            type=condition.type,
            met=False,
            observed_value=observed,
            threshold=threshold_value,
            evaluated_at=evaluated_at,
            data_quality=data_quality,
            evaluable=False,
            note="condition is not evaluable: required data is unavailable",
        )
    if condition.type not in SUPPORTED_TYPES:
        return ConditionResult(
            condition_id=condition.condition_id,
            type=condition.type,
            met=False,
            observed_value=observed,
            threshold=None,
            evaluated_at=evaluated_at,
            data_quality=data_quality,
            evaluable=False,
            note=f"unsupported condition type in this slice: {condition.type}",
        )
    low = _decimal(params.get("price_low"))
    high = _decimal(params.get("price_high"))
    if low is None or high is None:
        return ConditionResult(
            condition_id=condition.condition_id,
            type=condition.type,
            met=False,
            observed_value=observed,
            threshold=None,
            evaluated_at=evaluated_at,
            data_quality=data_quality,
            evaluable=False,
            note="PRICE_TOUCH_ZONE requires price_low and price_high",
        )
    threshold_value = f"{low}-{high}"
    met = low <= last_price <= high
    return ConditionResult(
        condition_id=condition.condition_id,
        type=condition.type,
        met=met,
        observed_value=observed,
        threshold=threshold_value,
        evaluated_at=evaluated_at,
        data_quality=data_quality,
        evaluable=True,
        note="price touched the zone" if met else "price is outside the zone",
    )


def evaluate_trigger(
    conditions: Iterable[Any],
    *,
    last_price: Decimal,
    snapshot_as_of: str,
    now: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    data_quality: str,
    snapshot_id: str,
) -> TriggerEvaluation:
    """Evaluate every condition; a stale snapshot can never trigger."""
    condition_list = tuple(conditions)
    snapshot_payload = {
        "snapshot_id": snapshot_id,
        "symbol": getattr(condition_list[0], "symbol", None) if condition_list else None,
        "last_price": str(last_price),
        "as_of": snapshot_as_of,
    }
    digest = snapshot_hash(snapshot_payload)
    age = (_parse(now) - _parse(snapshot_as_of)).total_seconds()
    stale = age > stale_after_seconds
    if stale:
        results = tuple(
            ConditionResult(
                condition_id=condition.condition_id,
                type=condition.type,
                met=False,
                observed_value=str(last_price),
                threshold=None,
                evaluated_at=now,
                data_quality="STALE",
                evaluable=False,
                note=f"snapshot is stale by {int(age)}s; no trigger is allowed",
            )
            for condition in condition_list
        )
        return TriggerEvaluation(
            met=False,
            stale=True,
            snapshot_id=snapshot_id,
            snapshot_hash=digest,
            as_of=snapshot_as_of,
            data_quality="STALE",
            results=results,
            reasons=(f"stale snapshot: {int(age)}s older than the {stale_after_seconds}s budget",),
        )

    results = tuple(
        evaluate_condition(
            condition,
            last_price=last_price,
            evaluated_at=now,
            data_quality=data_quality,
        )
        for condition in condition_list
    )
    met = bool(results) and all(result.met and result.evaluable for result in results)
    reasons: list[str] = []
    if not results:
        reasons.append("no trigger conditions are defined")
    for result in results:
        if not result.met:
            reasons.append(f"{result.condition_id}: {result.note}")
    return TriggerEvaluation(
        met=met,
        stale=False,
        snapshot_id=snapshot_id,
        snapshot_hash=digest,
        as_of=snapshot_as_of,
        data_quality=data_quality,
        results=results,
        reasons=tuple(reasons),
    )
