"""Pure position-management domain: protective stop touch and a full close.

Deterministic and I/O-free. The module reads a :class:`~oft.domain.oms.Position`
(or its contract payload), applies the one code-owned rule in this slice (a
recorded mark that touches the protective stop), and produces the closed
position payload plus the trade-log document. Identifiers are deterministic
functions of their inputs, so a replay with the same fixed clock reproduces the
same ids and values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from oft.domain.oms import (
    Fill,
    OrderSide,
    Position,
    PositionSide,
    PositionState,
    deterministic_id,
)
from oft.domain.pnl import (
    decimal_str,
    fees_total,
    gross_pnl_usd,
    money_str,
    net_pnl_usd,
    plain_money_str,
    quantize_money,
)

_SCHEMA_VERSION = "1.0.0"
FULL_CLOSE = "FULL_CLOSE"
STOP_TOUCH_RULE = "STOP_TOUCH"
FUNDING_REASON = "funding accrual not in this slice"


class PositionError(Exception):
    """A position-management operation is invalid for the current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProtectiveExit:
    """One deterministic stop-touch decision for an open position."""

    position_id: str
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    stop_price: Decimal
    mark_price: Decimal
    at: str
    rule: str = STOP_TOUCH_RULE

    @property
    def decision_id(self) -> str:
        return deterministic_id("MGD", f"{self.position_id}:{self.at}")

    @property
    def event_id(self) -> str:
        return deterministic_id("PEV", f"{self.position_id}:CLOSED:{self.at}")


@dataclass(frozen=True)
class CloseResult:
    """The closed position and its exact PnL terms."""

    position: Position
    gross_pnl_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    net_pnl_usd: Decimal

    def to_contract(self) -> dict[str, Any]:
        return self.position.to_contract()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def position_from_contract(payload: Mapping[str, Any]) -> Position:
    """Build the pure :class:`Position` from its persisted contract payload."""
    try:
        return Position(
            position_id=str(payload["position_id"]),
            account_id=str(payload["account_id"]),
            environment=str(payload["environment"]),
            symbol=str(payload["symbol"]),
            side=PositionSide(str(payload["side"])),
            state=str(payload["state"]),
            size=_decimal(payload["size"]),
            entry_price=_decimal(payload["entry_price"]),
            avg_entry_price=_decimal(payload["avg_entry_price"]),
            mark_price=_decimal_or_none(payload.get("mark_price")),
            notional_usd=_decimal(payload["notional_usd"]),
            stop_price=_decimal_or_none(payload.get("stop_price")),
            target_prices=tuple(_decimal(price) for price in (payload.get("target_prices") or ())),
            leverage=float(payload.get("leverage", 1.0)),
            margin_usd=_decimal(payload.get("margin_usd", "0")),
            risk_amount_usd=_decimal(payload.get("risk_amount_usd", "0")),
            risk_pct=float(payload.get("risk_pct", 0.0)),
            fees_usd=_decimal(payload.get("fees_usd", "0")),
            opened_at=str(payload["opened_at"]),
            updated_at=str(payload["updated_at"]),
            source_trigger_id=payload.get("source_trigger_id"),
            source_decision_id=payload.get("source_decision_id"),
            closed_at=payload.get("closed_at"),
            exit_price=_decimal_or_none(payload.get("exit_price")),
            realized_pnl_usd=_decimal_or_none(payload.get("realized_pnl_usd")),
            funding_usd=_decimal(payload.get("funding_usd", "0")),
            management_history=tuple(payload.get("management_history") or ()),
            version=int(payload.get("version", 1)),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise PositionError("DATA_UNAVAILABLE", f"the position payload is invalid: {exc}") from exc


def validate_stop_side(side: PositionSide, entry_price: Decimal, stop_price: Decimal) -> None:
    """A protective stop must sit on the losing side of the entry."""
    if side is PositionSide.LONG and stop_price >= entry_price:
        raise PositionError("POSITION_STATE_INVALID", "a LONG stop must be below the entry")
    if side is PositionSide.SHORT and stop_price <= entry_price:
        raise PositionError("POSITION_STATE_INVALID", "a SHORT stop must be above the entry")


def evaluate_protective_exit(
    position: Position,
    mark: Decimal,
    *,
    at: str,
) -> ProtectiveExit | None:
    """Return the deterministic stop-touch decision, or ``None`` when untouched.

    A LONG position is touched when ``mark <= stop``; a SHORT when
    ``mark >= stop``. A position without a protective stop can never be closed
    by this rule, and a stop on the wrong side of the entry fails closed.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be evaluated")
    if mark <= 0:
        raise PositionError("DATA_UNAVAILABLE", "the mark price must be positive")
    stop = position.stop_price
    if stop is None or stop <= 0:
        return None
    validate_stop_side(position.side, position.avg_entry_price, stop)
    touched = mark <= stop if position.side is PositionSide.LONG else mark >= stop
    if not touched:
        return None
    return ProtectiveExit(
        position_id=position.position_id,
        symbol=position.symbol,
        position_side=position.side,
        quantity=position.size,
        stop_price=stop,
        mark_price=mark,
        at=at,
    )


def close_full(position: Position, exit_fill: Fill, *, at: str) -> CloseResult:
    """Apply a full-close fill to an OPEN position and return the closed result.

    The existing row is updated in place by the caller; this function never
    creates a second position. The exit fee is added to the entry fee already
    carried by the position.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be closed")
    if exit_fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill symbol does not match")
    if exit_fill.quantity != position.size:
        raise PositionError("POSITION_STATE_INVALID", "only a full close is supported")
    expected_side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
    if exit_fill.side is not expected_side:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill side is wrong")

    gross = gross_pnl_usd(
        position.side.value,
        position.avg_entry_price,
        exit_fill.price,
        position.size,
    )
    fees = fees_total(position.fees_usd, exit_fill.fee)
    funding = Decimal("0")
    net = net_pnl_usd(gross, fees, funding)
    decision_id = deterministic_id("MGD", f"{position.position_id}:{at}")
    history = (
        *position.management_history,
        {
            "decision_id": decision_id,
            "action": FULL_CLOSE,
            "at": at,
            "executed_order_id": exit_fill.order_id,
            "note": "protective stop touched",
        },
    )
    closed = replace(
        position,
        state=PositionState.CLOSED.value,
        mark_price=exit_fill.price,
        fees_usd=fees,
        funding_usd=funding,
        realized_pnl_usd=net,
        updated_at=at,
        closed_at=at,
        exit_price=exit_fill.price,
        management_history=history,
        version=position.version + 1,
    )
    return CloseResult(
        position=closed,
        gross_pnl_usd=gross,
        fees_usd=fees,
        funding_usd=funding,
        net_pnl_usd=net,
    )


def trade_id_for(position_id: str) -> str:
    """The one deterministic trade-log id for a position."""
    return deterministic_id("TRD", position_id)


def trade_log_from_close(
    close: CloseResult,
    *,
    trade_id: str,
) -> dict[str, Any]:
    """Render the closed trade as a ``trade-log.schema.json`` document.

    Funding is the close's derived ``funding_usd`` term (zero in this slice),
    and every unmeasured field is named in ``data_quality.reasons`` rather than
    silently presented as a real value.
    """
    position = close.position
    entry_price = position.avg_entry_price
    exit_price = position.exit_price
    if exit_price is None:  # pragma: no cover - close_full always sets it
        raise PositionError("POSITION_STATE_INVALID", "the closed position has no exit price")
    notional = entry_price * position.size
    if notional > 0:
        return_pct = float(quantize_money(close.net_pnl_usd / notional * Decimal("100")))
    else:  # pragma: no cover - a position size is always positive
        return_pct = 0.0
    return {
        "schema_version": _SCHEMA_VERSION,
        "trade_id": trade_id,
        "position_id": position.position_id,
        "account_id": position.account_id,
        "environment": position.environment,
        "symbol": position.symbol,
        "side": position.side.value,
        "opened_at": position.opened_at,
        "closed_at": position.closed_at,
        "entry_avg_price": decimal_str(entry_price),
        "exit_avg_price": decimal_str(exit_price),
        "size": decimal_str(position.size),
        "gross_pnl_usd": money_str(close.gross_pnl_usd),
        "fees_usd": money_str(close.fees_usd),
        "funding_usd": plain_money_str(close.funding_usd),
        "net_pnl_usd": money_str(close.net_pnl_usd),
        "r_multiple": None,
        "mae_usd": "0",
        "mfe_usd": "0",
        "return_pct": return_pct,
        "signal": {
            "scan_id": None,
            "report_id": None,
            "opportunity_id": None,
            "trigger_id": None,
            "decision_id": None,
        },
        "decision_quality": {
            "matched_rules": None,
            "confidence": None,
            "notes": ("deterministic protective-exit rule; no LLM review in this slice"),
        },
        "outcome_tags": [],
        "review_decision_id": None,
        "data_quality": {
            "status": "PARTIAL",
            "reasons": [
                FUNDING_REASON,
                "MAE not measured in this slice",
                "MFE not measured in this slice",
                "R multiple not measured in this slice",
            ],
            "gaps": [],
            "last_event_at": position.closed_at,
            "coverage_start": position.opened_at,
        },
        "provenance": {
            "calc_metric_version": "pnl-v1",
            "prompt_version": 1,
            "llm_provider": "stub",
            "llm_model": "deterministic-protective-exit",
        },
    }


__all__ = [
    "FULL_CLOSE",
    "FUNDING_REASON",
    "STOP_TOUCH_RULE",
    "CloseResult",
    "PositionError",
    "ProtectiveExit",
    "close_full",
    "evaluate_protective_exit",
    "position_from_contract",
    "trade_id_for",
    "trade_log_from_close",
    "validate_stop_side",
]
