"""Deterministic trigger evaluation over the closed condition set.

Only code evaluates conditions. ``evaluate_trigger`` freezes a
:class:`TriggerSnapshot` and evaluates every condition through slice 16's
:func:`oft.domain.conditions.evaluate_condition`. A stale snapshot, a condition
that is not evaluable, or an empty condition set can never trigger (ADR-0011).

The module stays pure: standard library plus :mod:`oft.domain.conditions`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from oft.domain.conditions import EvaluationContext
from oft.domain.conditions import evaluate_condition as evaluate_registry_condition

SUPPORTED_TYPES = frozenset({"PRICE_TOUCH_ZONE"})
DEFAULT_STALE_AFTER_SECONDS = 60
CONTRACT_DATA_QUALITIES = frozenset({"COMPLETE", "PARTIAL", "STALE", "MISSING", "ESTIMATED"})
_STALE_DATA_QUALITIES = frozenset({"STALE"})


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
class TriggerSnapshot:
    """The exact frozen market state one trigger evaluation is allowed to see.

    ``as_of`` is the ceiling: every value and every bar in ``*_series`` must have
    been observed at or before it. ``None`` means the input is absent, never
    zero. Decimals are exact.
    """

    symbol: str
    as_of: str
    last_price: Decimal | None = None
    previous_price: Decimal | None = None
    tick_size: Decimal | None = None
    bar_timestamps: tuple[str, ...] | None = None
    price_series: tuple[Decimal, ...] | None = None
    delta_series: tuple[Decimal, ...] | None = None
    cvd_series: tuple[Decimal, ...] | None = None
    volume_series: tuple[Decimal, ...] | None = None
    oi_series: tuple[Decimal, ...] | None = None
    delta: Decimal | None = None
    cvd: Decimal | None = None
    oi_delta_pct: Decimal | None = None
    funding_rate: Decimal | None = None
    liquidation_notional: Decimal | None = None
    volume: Decimal | None = None
    footprint_imbalance: Decimal | None = None
    session_state: str | None = None
    data_quality: str = "COMPLETE"
    quality_reasons: tuple[str, ...] = ()
    snapshot_id: str = ""


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


def contract_data_quality(value: object) -> str:
    """Map a recorded quality label onto the contract's ``dataQuality`` enum."""
    if isinstance(value, str) and value in CONTRACT_DATA_QUALITIES:
        return value
    return "MISSING"


def evaluation_context(snapshot: TriggerSnapshot) -> EvaluationContext:
    """The exact slice-16 context for a frozen snapshot (no I/O, no reformatting)."""
    return EvaluationContext(
        as_of=snapshot.as_of,
        last_price=snapshot.last_price,
        previous_price=snapshot.previous_price,
        tick_size=snapshot.tick_size,
        delta=snapshot.delta,
        cvd=snapshot.cvd,
        cvd_series=snapshot.cvd_series,
        price_series=snapshot.price_series,
        oi_delta_pct=snapshot.oi_delta_pct,
        funding_rate=snapshot.funding_rate,
        liquidation_notional=snapshot.liquidation_notional,
        volume=snapshot.volume,
        footprint_imbalance=snapshot.footprint_imbalance,
        session_state=snapshot.session_state,
        bar_timestamps=snapshot.bar_timestamps,
        delta_series=snapshot.delta_series,
        volume_series=snapshot.volume_series,
        oi_series=snapshot.oi_series,
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _series_text(series: tuple[Decimal, ...] | None) -> list[str] | None:
    return None if series is None else [format(value, "f") for value in series]


def _conditions_identity(conditions: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {
            "condition_id": condition.condition_id,
            "type": condition.type,
            "params": dict(condition.params),
            "window_start": condition.window_start,
            "window_end": condition.window_end,
        }
        for condition in conditions
    ]


def _snapshot_identity(
    snapshot: TriggerSnapshot,
    *,
    symbol: str,
    conditions: tuple[Any, ...],
) -> dict[str, Any]:
    """Canonical hash input: every frozen value plus the condition parameters.

    The full series participate (not only their endpoints) so two snapshots
    with equal endpoints but different interiors never share an idempotency
    key. The condition params participate so a changed condition set cannot
    reuse a prior trigger.
    """
    return {
        "symbol": symbol,
        "as_of": snapshot.as_of,
        "last_price": _decimal_text(snapshot.last_price),
        "previous_price": _decimal_text(snapshot.previous_price),
        "tick_size": _decimal_text(snapshot.tick_size),
        "metrics": {
            "delta": _decimal_text(snapshot.delta),
            "cvd": _decimal_text(snapshot.cvd),
            "oi_delta_pct": _decimal_text(snapshot.oi_delta_pct),
            "funding_rate": _decimal_text(snapshot.funding_rate),
            "liquidation_notional": _decimal_text(snapshot.liquidation_notional),
            "volume": _decimal_text(snapshot.volume),
            "footprint_imbalance": _decimal_text(snapshot.footprint_imbalance),
        },
        "series": {
            "bar_timestamps": (
                None if snapshot.bar_timestamps is None else list(snapshot.bar_timestamps)
            ),
            "price": _series_text(snapshot.price_series),
            "delta": _series_text(snapshot.delta_series),
            "cvd": _series_text(snapshot.cvd_series),
            "volume": _series_text(snapshot.volume_series),
            "open_interest": _series_text(snapshot.oi_series),
        },
        "session_state": snapshot.session_state,
        "data_quality": snapshot.data_quality,
        "conditions": _conditions_identity(conditions),
    }


def _price_only_snapshot(
    *,
    symbol: str | None,
    last_price: Decimal | None,
    snapshot_as_of: str | None,
    data_quality: str,
    snapshot_id: str,
) -> TriggerSnapshot:
    if last_price is None or snapshot_as_of is None:
        raise ValueError("evaluate_trigger requires a snapshot or last_price + snapshot_as_of")
    return TriggerSnapshot(
        symbol=symbol or "",
        as_of=snapshot_as_of,
        last_price=last_price,
        data_quality=data_quality,
        snapshot_id=snapshot_id,
    )


def _stale_reason(
    snapshot: TriggerSnapshot,
    *,
    now: str,
    stale_after_seconds: int,
) -> str | None:
    """Return why the snapshot may not trigger, or ``None`` when it is fresh."""
    if snapshot.last_price is None:
        return "stale snapshot: last_price is unavailable"
    try:
        age = (_parse(now) - _parse(snapshot.as_of)).total_seconds()
    except (TypeError, ValueError):
        return "stale snapshot: as_of is malformed"
    if age < 0:
        # A clock-ahead or misconfigured as_of is not fresh: admitting it
        # would also raise the no-lookahead ceiling above the evaluation clock.
        return "stale snapshot: as_of is in the future"
    if age > stale_after_seconds:
        return f"stale snapshot: {int(age)}s older than the {stale_after_seconds}s budget"
    if contract_data_quality(snapshot.data_quality) in _STALE_DATA_QUALITIES:
        reason = "stale snapshot: data quality is STALE"
        if snapshot.quality_reasons:
            reason += " (" + "; ".join(snapshot.quality_reasons) + ")"
        return reason
    return None


def _stale_results(
    conditions: tuple[Any, ...], *, now: str, reason: str
) -> tuple[ConditionResult, ...]:
    return tuple(
        ConditionResult(
            condition_id=condition.condition_id,
            type=condition.type,
            met=False,
            observed_value=None,
            threshold=None,
            evaluated_at=now,
            data_quality="STALE",
            evaluable=False,
            note=reason,
        )
        for condition in conditions
    )


def _condition_result(result: Any, *, fallback_at: str) -> ConditionResult:
    evaluated_at = result.evaluated_at or fallback_at
    note = result.note or result.unavailable_reason or ""
    return ConditionResult(
        condition_id=result.condition_id,
        type=result.type,
        met=result.met,
        observed_value=result.observed_value,
        threshold=result.threshold,
        evaluated_at=evaluated_at,
        data_quality=contract_data_quality(result.data_quality),
        evaluable=result.evaluable,
        note=note,
    )


def evaluate_trigger(
    conditions: Iterable[Any],
    snapshot: TriggerSnapshot | None = None,
    *,
    symbol: str | None = None,
    now: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    snapshot_id: str = "",
    last_price: Decimal | None = None,
    snapshot_as_of: str | None = None,
    data_quality: str = "COMPLETE",
) -> TriggerEvaluation:
    """Evaluate every condition against one frozen snapshot.

    A snapshot older than ``stale_after_seconds``, missing its mark price, or
    recorded as ``STALE`` can never trigger. Otherwise a trigger requires every
    condition to be both ``evaluable`` and ``met``. Passing ``last_price`` and
    ``snapshot_as_of`` instead of a snapshot is the backward-compatible
    price-only path: conditions whose data is absent report ``evaluable=false``.
    """
    condition_list = tuple(conditions)
    legacy = snapshot is None
    frozen = (
        snapshot
        if snapshot is not None
        else _price_only_snapshot(
            symbol=symbol,
            last_price=last_price,
            snapshot_as_of=snapshot_as_of,
            data_quality=data_quality,
            snapshot_id=snapshot_id,
        )
    )
    resolved_symbol = symbol or frozen.symbol
    digest = snapshot_hash(
        _snapshot_identity(frozen, symbol=resolved_symbol, conditions=condition_list)
    )
    # An empty id is never persisted: the contract requires a non-empty
    # ``market_snapshot_id``, so derive a deterministic one from the snapshot
    # identity. This also hardens custom providers that omit the id.
    resolved_snapshot_id = snapshot_id or frozen.snapshot_id or f"SNAP-{digest[:16].upper()}"
    stale_reason = _stale_reason(frozen, now=now, stale_after_seconds=stale_after_seconds)
    if stale_reason is not None:
        return TriggerEvaluation(
            met=False,
            stale=True,
            snapshot_id=resolved_snapshot_id,
            snapshot_hash=digest,
            as_of=frozen.as_of,
            data_quality="STALE",
            results=_stale_results(condition_list, now=now, reason=stale_reason),
            reasons=(stale_reason,),
        )

    if legacy:
        # The price-only wrapper keeps the slice-11 contract: a lone
        # PRICE_TOUCH_ZONE is evaluable from the mark alone, and any other type
        # is reported unsupported. Full registry evaluation requires a
        # snapshot with the recorded series and contract specs.
        frozen_price = frozen.last_price
        if frozen_price is None:  # pragma: no cover - the stale check above returns first
            raise ValueError("the price-only path requires a last_price")
        legacy_results = tuple(
            evaluate_condition(
                condition,
                last_price=frozen_price,
                evaluated_at=now,
                data_quality=contract_data_quality(frozen.data_quality),
            )
            for condition in condition_list
        )
        legacy_met = bool(legacy_results) and all(
            result.met and result.evaluable for result in legacy_results
        )
        legacy_reasons: list[str] = []
        if not legacy_results:
            legacy_reasons.append("no trigger conditions are defined")
        for result in legacy_results:
            if not result.met:
                legacy_reasons.append(f"{result.condition_id}: {result.note}")
        return TriggerEvaluation(
            met=legacy_met,
            stale=False,
            snapshot_id=resolved_snapshot_id,
            snapshot_hash=digest,
            as_of=frozen.as_of,
            data_quality=contract_data_quality(frozen.data_quality),
            results=legacy_results,
            reasons=tuple(legacy_reasons),
        )

    context = evaluation_context(frozen)
    evaluations = tuple(
        evaluate_registry_condition(condition, context) for condition in condition_list
    )
    met = bool(evaluations) and all(item.evaluable and item.met for item in evaluations)
    reasons: list[str] = []
    if not evaluations:
        reasons.append("no trigger conditions are defined")
    for item in evaluations:
        if not item.evaluable:
            reasons.append(f"{item.condition_id}: {item.unavailable_reason or item.note}")
        elif not item.met:
            reasons.append(f"{item.condition_id}: {item.note}")
    return TriggerEvaluation(
        met=met,
        stale=False,
        snapshot_id=resolved_snapshot_id,
        snapshot_hash=digest,
        as_of=frozen.as_of,
        data_quality=contract_data_quality(frozen.data_quality),
        results=tuple(_condition_result(item, fallback_at=now) for item in evaluations),
        reasons=tuple(reasons),
    )


# -- legacy price-only evaluator -------------------------------------------
# Kept for callers that still evaluate a lone PRICE_TOUCH_ZONE against a price.
# The trigger path itself uses slice 16's registry evaluator above.


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
    """Evaluate one legacy ``PRICE_TOUCH_ZONE`` against a frozen price."""
    params = condition.params
    observed = str(last_price)
    if not condition.evaluable:
        return ConditionResult(
            condition_id=condition.condition_id,
            type=condition.type,
            met=False,
            observed_value=observed,
            threshold=None,
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
    met = low <= last_price <= high
    return ConditionResult(
        condition_id=condition.condition_id,
        type=condition.type,
        met=met,
        observed_value=observed,
        threshold=f"{low}-{high}",
        evaluated_at=evaluated_at,
        data_quality=data_quality,
        evaluable=True,
        note="price touched the zone" if met else "price is outside the zone",
    )


__all__ = [
    "CONTRACT_DATA_QUALITIES",
    "ConditionResult",
    "DEFAULT_STALE_AFTER_SECONDS",
    "SUPPORTED_TYPES",
    "TriggerEvaluation",
    "TriggerSnapshot",
    "contract_data_quality",
    "evaluate_condition",
    "evaluate_trigger",
    "evaluation_context",
    "snapshot_hash",
]
