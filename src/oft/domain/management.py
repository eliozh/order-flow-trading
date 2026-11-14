"""Semantic validation and deterministic sizing for position management (slice 25).

JSON Schema cannot express ``HOLD`` carrying no changes, a partial-close
fraction range, an add's required risk percentage, or a stop on the correct side
of the position. This module is pure: it validates the provider-facing
``position_management`` payload against the position state and turns a fraction
into one reduce-only quantity. It never trusts a provider-supplied size, price,
or execution outcome as authority.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from oft.domain.oms import Position, PositionSide
from oft.domain.pnl import MAX_ABS_PRICE, json_decimal, quantize_money
from oft.domain.positions import PositionError, validate_stop_side
from oft.domain.risk import ManagementAction


@dataclass(frozen=True)
class ManagementIssue:
    """One semantic violation in a management recommendation."""

    field: str
    code: str
    message: str


class ManagementSizingError(Exception):
    """A fraction cannot become a usable reduce-only quantity."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PartialClosePlan:
    """A partial-close fraction resolved to one executable quantity.

    ``escalated`` is ``True`` when the requested slice would leave a remainder
    below the recorded step/minimum; the caller then executes a ``FULL_CLOSE``
    and records ``reason`` so the escalation is never silent.
    """

    quantity: Decimal
    escalated: bool
    reason: str | None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _target_issues(
    position: Position,
    targets: Sequence[object],
    *,
    field: str,
) -> list[ManagementIssue]:
    """Validate every target and require a sensible profit-side ordering.

    A LONG target must sit above the average entry; a SHORT target below it. The
    sequence must move strictly away from the entry in list order. A target that
    is absent or unparseable is named, never silently dropped.
    """
    issues: list[ManagementIssue] = []
    values: list[Decimal] = []
    for index, raw in enumerate(targets):
        value = json_decimal(raw)
        if value is None or value <= 0 or value > MAX_ABS_PRICE:
            issues.append(
                ManagementIssue(
                    f"{field}[{index}]",
                    "TARGET_INVALID",
                    f"{field}[{index}] must be a positive decimal price",
                )
            )
            continue
        profit_side = (
            value > position.avg_entry_price
            if position.side is PositionSide.LONG
            else value < position.avg_entry_price
        )
        if not profit_side:
            issues.append(
                ManagementIssue(
                    f"{field}[{index}]",
                    "TARGET_SIDE",
                    f"{field}[{index}] must be on the profit side of the entry",
                )
            )
            continue
        if values:
            ordered = (
                value > values[-1] if position.side is PositionSide.LONG else value < values[-1]
            )
            if not ordered:
                issues.append(
                    ManagementIssue(
                        f"{field}[{index}]",
                        "TARGET_ORDER",
                        f"{field} must move away from the entry in list order",
                    )
                )
                continue
        values.append(value)
    return issues


def _stop_issue(
    position: Position,
    raw_stop: object,
    *,
    reference: Decimal,
) -> list[ManagementIssue]:
    """Validate a proposed stop: parseable, positive, and on the losing side."""
    if raw_stop is None:
        return []
    stop = json_decimal(raw_stop)
    if stop is None or stop <= 0 or stop > MAX_ABS_PRICE:
        return [
            ManagementIssue(
                "position_management.new_stop_price",
                "STOP_INVALID",
                "new_stop_price must be a positive decimal price",
            )
        ]
    try:
        validate_stop_side(position.side, reference, stop)
    except PositionError as exc:
        return [
            ManagementIssue(
                "position_management.new_stop_price",
                "STOP_SIDE",
                exc.message,
            )
        ]
    return []


def validate_position_management(
    payload: Mapping[str, Any],
    position: Position,
    *,
    mark_price: Decimal | None = None,
) -> tuple[ManagementIssue, ...]:
    """Validate one ``position_management`` payload against the position state.

    The provider's ``action`` decides which fields must be present and which
    must be absent. Every violation is returned (not just the first) so the
    caller can name the failing field(s) in the rejection and the single repair.
    """
    issues: list[ManagementIssue] = []
    raw_action = payload.get("action")
    try:
        action = ManagementAction(str(raw_action))
    except ValueError:
        return (
            ManagementIssue(
                "position_management.action",
                "ACTION_INVALID",
                f"unknown management action {raw_action!r}",
            ),
        )
    reference = mark_price or position.mark_price or position.avg_entry_price
    fraction = payload.get("close_fraction")
    add_risk = payload.get("add_risk_pct")
    raw_stop = payload.get("new_stop_price")
    raw_targets = payload.get("new_targets")
    targets: list[object] = []
    if raw_targets is not None:
        if not isinstance(raw_targets, list):
            issues.append(
                ManagementIssue(
                    "position_management.new_targets",
                    "TARGET_INVALID",
                    "new_targets must be a list",
                )
            )
        else:
            targets = list(raw_targets)

    issues.extend(_stop_issue(position, raw_stop, reference=reference))
    issues.extend(_target_issues(position, targets, field="position_management.new_targets"))

    if action is ManagementAction.HOLD:
        if fraction is not None:
            issues.append(
                ManagementIssue(
                    "position_management.close_fraction",
                    "HOLD_CARRIES_CHANGES",
                    "HOLD must not carry a close fraction",
                )
            )
        if add_risk is not None:
            issues.append(
                ManagementIssue(
                    "position_management.add_risk_pct",
                    "HOLD_CARRIES_CHANGES",
                    "HOLD must not carry an add risk percentage",
                )
            )
        if raw_stop is not None:
            issues.append(
                ManagementIssue(
                    "position_management.new_stop_price",
                    "HOLD_CARRIES_CHANGES",
                    "HOLD must not move the stop",
                )
            )
        if targets:
            issues.append(
                ManagementIssue(
                    "position_management.new_targets",
                    "HOLD_CARRIES_CHANGES",
                    "HOLD must not replace the targets",
                )
            )
    elif action is ManagementAction.PARTIAL_CLOSE:
        if fraction is None:
            issues.append(
                ManagementIssue(
                    "position_management.close_fraction",
                    "CLOSE_FRACTION_REQUIRED",
                    "PARTIAL_CLOSE requires a close fraction",
                )
            )
        else:
            value = _finite_float(fraction)
            if value is None or not 0 < value < 1:
                issues.append(
                    ManagementIssue(
                        "position_management.close_fraction",
                        "CLOSE_FRACTION_RANGE",
                        "close_fraction must be strictly between 0 and 1",
                    )
                )
        if add_risk is not None:
            issues.append(
                ManagementIssue(
                    "position_management.add_risk_pct",
                    "ACTION_MISMATCH",
                    "PARTIAL_CLOSE must not carry an add risk percentage",
                )
            )
    elif action is ManagementAction.FULL_CLOSE:
        if fraction is not None:
            issues.append(
                ManagementIssue(
                    "position_management.close_fraction",
                    "ACTION_MISMATCH",
                    "FULL_CLOSE must not carry a close fraction",
                )
            )
        if add_risk is not None:
            issues.append(
                ManagementIssue(
                    "position_management.add_risk_pct",
                    "ACTION_MISMATCH",
                    "FULL_CLOSE must not carry an add risk percentage",
                )
            )
    else:  # ADD
        if fraction is not None:
            issues.append(
                ManagementIssue(
                    "position_management.close_fraction",
                    "ACTION_MISMATCH",
                    "ADD must not carry a close fraction",
                )
            )
        if add_risk is None:
            issues.append(
                ManagementIssue(
                    "position_management.add_risk_pct",
                    "ADD_RISK_REQUIRED",
                    "ADD requires a positive add_risk_pct",
                )
            )
        else:
            value = _finite_float(add_risk)
            if value is None or not 0 < value <= 100:
                issues.append(
                    ManagementIssue(
                        "position_management.add_risk_pct",
                        "ADD_RISK_RANGE",
                        "add_risk_pct must be greater than 0 and at most 100",
                    )
                )
        if raw_stop is not None:
            issues.append(
                ManagementIssue(
                    "position_management.new_stop_price",
                    "ADD_STOP_UNSUPPORTED",
                    "ADD must not move the stop; manage the stop on a reduce action",
                )
            )
    return tuple(issues)


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def plan_partial_close(
    *,
    size: Decimal,
    fraction: Decimal,
    mark_price: Decimal,
    step_size: Decimal,
    min_notional: Decimal,
) -> PartialClosePlan:
    """Turn a partial-close fraction into one executable quantity.

    The quantity is rounded down to the recorded step so the broker's step check
    always passes. A slice that rounds to zero is rejected; a slice that would
    leave a remainder below the recorded step or minimum notional escalates to a
    full close and records the reason (never a silent zero-filled remainder).
    """
    if size <= 0:
        raise ManagementSizingError("the position size must be positive")
    if mark_price <= 0:
        raise ManagementSizingError("the mark price must be positive")
    if not 0 < fraction < 1:
        raise ManagementSizingError("a partial-close fraction must be between 0 and 1")
    try:
        quantity = round_down_to_step(size * fraction, step_size)
    except (ArithmeticError, ValueError) as exc:
        raise ManagementSizingError(
            "the position size and fraction make an implausible quantity"
        ) from exc
    if quantity <= 0:
        raise ManagementSizingError("the partial-close fraction rounds to zero")
    if quantity >= size:
        return PartialClosePlan(
            quantity=size,
            escalated=True,
            reason="the requested slice is the whole position; escalated to FULL_CLOSE",
        )
    remainder = size - quantity
    try:
        remainder_notional = quantize_money(remainder * mark_price)
    except (ArithmeticError, ValueError) as exc:
        raise ManagementSizingError("the remainder makes an implausible notional") from exc
    below_step = step_size > 0 and remainder < step_size
    below_notional = min_notional > 0 and remainder_notional < min_notional
    if below_step or below_notional:
        return PartialClosePlan(
            quantity=size,
            escalated=True,
            reason=(
                "the partial-close remainder is below the recorded step or minimum "
                "notional; escalated to FULL_CLOSE"
            ),
        )
    return PartialClosePlan(quantity=quantity, escalated=False, reason=None)


__all__ = [
    "ManagementIssue",
    "ManagementSizingError",
    "PartialClosePlan",
    "plan_partial_close",
    "round_down_to_step",
    "validate_position_management",
]
