"""The read-only risk-cap utilization snapshot (slice 34).

Only the paper account row, its OPEN positions, and the equity computed from
them feed the snapshot. Nothing here writes or seeds a row, and no corruption
raises: an unreadable account balance or position payload, an out-of-float-range
number, and a position arithmetic total the decimal context cannot represent
all degrade to an explicit UNAVAILABLE/PARTIAL result with a note, never a
fabricated zero and never a 500 for a settings read.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from oft.application.paper_account import DEFAULT_ACCOUNT_ID, compute_equity
from oft.domain.risk import CAP_FIELDS, RiskCaps
from oft.ports.store import StorePort

# The caps with no live measurement in this slice; they are reported with
# ``used: null`` and an explicit reason, never a fabricated zero.
_UNMEASURED_RISK_CAPS = frozenset({"max_slippage_bps", "max_spread_bps", "stale_mark_seconds"})
_RISK_CAP_UNITS: Mapping[str, str] = {
    "per_trade_risk_pct": "%",
    "max_concurrent_positions": "positions",
    "daily_loss_pct": "%",
    "max_leverage": "x",
    "max_notional_pct": "%",
    "max_slippage_bps": "bps",
    "max_spread_bps": "bps",
    "stale_mark_seconds": "s",
}
_UNMEASURED_NOTE = "no live measurement exists for this cap"
_DAILY_LOSS_NOTE = (
    "no per-UTC-day realized-PnL source exists in this slice; daily loss is not measured"
)
_UNREADABLE_ACCOUNT_NOTE = "paper account row is unreadable"
_UNREADABLE_POSITION_NOTE = "an open position payload is unreadable; utilization cannot be measured"
_UNREADABLE_POSITION_COUNT_NOTE = (
    "an open position payload is unreadable; the OPEN row count is still exact"
)
# Equity-dependent failures are named precisely so the notional note never
# blames the position payload for a missing mark or an unrepresentable total.
_EQUITY_STALE_INPUT = "equity"
_EQUITY_UNCOMPUTABLE_NOTE = "equity could not be computed"
_EQUITY_NOT_POSITIVE_NOTE = "equity is not positive"
_NOTIONAL_UNCOMPUTABLE_NOTE = "position notional could not be computed"


@dataclass(frozen=True)
class RiskUtilizationMetric:
    """One cap's read-only utilization and its provenance."""

    used: float | None
    cap: float
    unit: str
    source: str
    quality: str
    note: str | None = None

    def to_contract(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "cap": self.cap,
            "unit": self.unit,
            "source": self.source,
            "quality": self.quality,
            "note": self.note,
        }


@dataclass(frozen=True)
class RiskUtilization:
    """The read-only utilization snapshot; never mutates anything."""

    source: str
    quality: str
    stale_inputs: tuple[str, ...]
    metrics: dict[str, RiskUtilizationMetric]

    def to_contract(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "quality": self.quality,
            "stale_inputs": list(self.stale_inputs),
            "metrics": {field: metric.to_contract() for field, metric in self.metrics.items()},
        }


def build_risk_utilization(store: StorePort, caps: RiskCaps) -> RiskUtilization:
    """Read-only utilization; never seeds, writes, or raises on corruption.

    The lifetime trade-log totals are not a daily loss measurement, so no
    utilization metric consumes them and this path never performs the unbounded
    ``trade_logs`` scan that ``PaperAccountService.closed_totals`` costs. An
    account that does not exist stays unavailable rather than being seeded as a
    read side effect.

    Corruption policy: an unreadable account balance, an OPEN position whose
    payload is not a readable JSON object, a number outside the float range, or
    a size × mark total the decimal context cannot represent degrades to an
    explicit UNAVAILABLE/PARTIAL result with a note. A failed equity total
    leaves the notional metric unavailable while the readable per-position
    metrics and the exact OPEN row count keep reporting.
    """
    account = store.get_paper_account(DEFAULT_ACCOUNT_ID)
    if account is None:
        return unavailable_utilization(caps, "paper account is not initialized")
    try:
        cash = Decimal(account.balance)
        if not cash.is_finite():
            raise ValueError("paper account balance must be finite")
    except (ArithmeticError, TypeError, ValueError):
        return unavailable_utilization(caps, _UNREADABLE_ACCOUNT_NOTE)

    rows = store.list_positions(state="OPEN", limit=None)
    positions: list[dict[str, Any]] = []
    unreadable_positions = 0
    for record in rows:
        try:
            payload = json.loads(record.payload_json)
        except (TypeError, ValueError):
            unreadable_positions += 1
            continue
        if not isinstance(payload, Mapping):
            unreadable_positions += 1
            continue
        positions.append(dict(payload))

    equity: Decimal | None
    try:
        equity, view = compute_equity(cash, positions)
    except (ArithmeticError, TypeError, ValueError):
        # Every payload is readable but the marked-to-market total is not
        # representable in the decimal context (e.g. size × mark beyond the
        # precision/range). Equity is untrusted, never a fabricated zero.
        equity = None
        stale = [_EQUITY_STALE_INPUT]
    else:
        stale = [str(name) for name in view.stale_inputs]
    if unreadable_positions:
        stale.append("position_payload")
    stale_inputs = tuple(stale)
    # Nothing readable at all is stronger than a partial snapshot; a partial
    # snapshot is still never reported as complete.
    if unreadable_positions and not positions:
        quality = "UNAVAILABLE"
    elif stale_inputs:
        quality = "PARTIAL"
    else:
        quality = "COMPLETE"

    per_trade, per_trade_complete = _max_position_number(positions, "risk_pct")
    leverage, leverage_complete = _max_position_number(positions, "leverage")
    notional, notional_failure = _position_notional(positions)
    notional_pct, notional_note = _notional_percent(
        notional, notional_failure, equity, unreadable=bool(unreadable_positions)
    )

    measured: dict[str, RiskUtilizationMetric] = {}
    per_trade_unavailable = unreadable_positions or not per_trade_complete
    measured["per_trade_risk_pct"] = _metric(
        caps,
        "per_trade_risk_pct",
        None if per_trade_unavailable else per_trade,
        quality,
        note=(_UNREADABLE_POSITION_NOTE if unreadable_positions else _missing_key_note("risk_pct")),
    )
    if unreadable_positions:
        # The row count is knowable even when a payload is not.
        measured["max_concurrent_positions"] = RiskUtilizationMetric(
            used=float(len(rows)),
            cap=_cap_float(caps, "max_concurrent_positions"),
            unit=_RISK_CAP_UNITS["max_concurrent_positions"],
            source="paper_account",
            quality="PARTIAL",
            note=_UNREADABLE_POSITION_COUNT_NOTE,
        )
    else:
        measured["max_concurrent_positions"] = _metric(
            caps,
            "max_concurrent_positions",
            float(len(rows)),
            quality,
        )
    # Lifetime realized PnL is not a daily-loss measurement. Until a per-UTC-day
    # source exists (the entry gate records the same gap), the metric is
    # unavailable rather than a mislabelled lifetime figure.
    measured["daily_loss_pct"] = RiskUtilizationMetric(
        used=None,
        cap=_cap_float(caps, "daily_loss_pct"),
        unit=_RISK_CAP_UNITS["daily_loss_pct"],
        source="none",
        quality="UNAVAILABLE",
        note=_DAILY_LOSS_NOTE,
    )
    leverage_unavailable = unreadable_positions or not leverage_complete
    measured["max_leverage"] = _metric(
        caps,
        "max_leverage",
        None if leverage_unavailable else leverage,
        quality,
        note=(_UNREADABLE_POSITION_NOTE if unreadable_positions else _missing_key_note("leverage")),
    )
    notional_used = notional_pct
    measured["max_notional_pct"] = _metric(
        caps,
        "max_notional_pct",
        notional_used,
        quality if notional_used is not None else "UNAVAILABLE",
        note=notional_note,
    )
    for field in _UNMEASURED_RISK_CAPS:
        measured[field] = RiskUtilizationMetric(
            used=None,
            cap=_cap_float(caps, field),
            unit=_RISK_CAP_UNITS[field],
            source="none",
            quality="UNAVAILABLE",
            note=_UNMEASURED_NOTE,
        )
    return RiskUtilization(
        source="paper_account",
        quality=quality,
        stale_inputs=stale_inputs,
        metrics={field: measured[field] for field in CAP_FIELDS},
    )


def unavailable_utilization(caps: RiskCaps, reason: str) -> RiskUtilization:
    """Every metric explicitly unavailable, with ``reason`` as the stale input."""
    metrics: dict[str, RiskUtilizationMetric] = {}
    for field in CAP_FIELDS:
        metrics[field] = RiskUtilizationMetric(
            used=None,
            cap=_cap_float(caps, field),
            unit=_RISK_CAP_UNITS[field],
            source="none",
            quality="UNAVAILABLE",
            note=_UNMEASURED_NOTE if field in _UNMEASURED_RISK_CAPS else reason,
        )
    return RiskUtilization(
        source="none",
        quality="UNAVAILABLE",
        stale_inputs=(reason,),
        metrics=metrics,
    )


def _cap_float(caps: RiskCaps, field: str) -> float:
    return float(getattr(caps, field))


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _max_position_number(positions: list[dict[str, Any]], key: str) -> tuple[float | None, bool]:
    """The largest finite numeric ``key`` and whether every row was readable.

    ``(0.0, True)`` is reserved for the genuinely empty position set. A single
    row that omits, carries a bool, or supplies a non-numeric/non-finite value
    makes the whole aggregate unmeasurable: ``(None, False)``, never a
    fabricated maximum over the remaining rows.
    """
    if not positions:
        return 0.0, True
    values: list[float] = []
    for position in positions:
        raw = position.get(key)
        if raw is None or isinstance(raw, bool):
            return None, False
        try:
            number = float(raw)
        except (ArithmeticError, TypeError, ValueError):
            # ``float`` raises OverflowError (an ArithmeticError) for a valid
            # JSON integer whose magnitude exceeds the float range.
            return None, False
        if not math.isfinite(number):
            return None, False
        values.append(number)
    return max(values), True


def _missing_key_note(key: str) -> str:
    """The note naming the open position key that could not be measured."""
    return f"{key} is missing or not finite on an open position"


def _position_notional(positions: list[dict[str, Any]]) -> tuple[Decimal | None, str | None]:
    """Signed-agnostic position notional plus the precise reason it is unknown.

    ``(None, note)`` on the first missing/non-finite size or mark, or when the
    multiply/accumulate is not representable in the decimal context. The note
    names the actual cause so it is never attributed to a corrupt payload.
    """
    total = Decimal("0")
    for position in positions:
        size = _decimal(position.get("size"))
        if size is None:
            return None, _missing_key_note("size")
        mark = _decimal(position.get("mark_price"))
        if mark is None:
            return None, _missing_key_note("mark_price")
        try:
            total += abs(size * mark)
        except ArithmeticError:
            return None, _NOTIONAL_UNCOMPUTABLE_NOTE
    return total, None


def _notional_percent(
    notional: Decimal | None,
    notional_failure: str | None,
    equity: Decimal | None,
    *,
    unreadable: bool,
) -> tuple[float | None, str | None]:
    """The notional/equity percentage plus the precise reason it is unavailable."""
    if unreadable:
        return None, _UNREADABLE_POSITION_NOTE
    if equity is None:
        return None, _EQUITY_UNCOMPUTABLE_NOTE
    if notional is None:
        return None, notional_failure or _NOTIONAL_UNCOMPUTABLE_NOTE
    if equity <= 0:
        return None, _EQUITY_NOT_POSITIVE_NOTE
    try:
        percentage = float(notional / equity * Decimal("100"))
    except (ArithmeticError, TypeError, ValueError):
        return None, _NOTIONAL_UNCOMPUTABLE_NOTE
    if not math.isfinite(percentage):
        # Starlette serializes with ``allow_nan=False``; an inf/nan float in the
        # response would itself become a 500.
        return None, _NOTIONAL_UNCOMPUTABLE_NOTE
    return percentage, None


def _metric(
    caps: RiskCaps,
    field: str,
    used: float | None,
    quality: str,
    *,
    note: str | None = None,
) -> RiskUtilizationMetric:
    if used is None:
        return RiskUtilizationMetric(
            used=None,
            cap=_cap_float(caps, field),
            unit=_RISK_CAP_UNITS[field],
            source="none",
            quality="UNAVAILABLE",
            note=note or "no usable measurement in the paper account",
        )
    return RiskUtilizationMetric(
        used=used,
        cap=_cap_float(caps, field),
        unit=_RISK_CAP_UNITS[field],
        source="paper_account",
        quality=quality,
        note=None,
    )


__all__ = [
    "RiskUtilization",
    "RiskUtilizationMetric",
    "build_risk_utilization",
    "unavailable_utilization",
]
