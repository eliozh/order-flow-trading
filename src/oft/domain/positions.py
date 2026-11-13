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
    MAX_ABS_MONEY,
    MAX_ABS_PRICE,
    MAX_ABS_QUANTITY,
    ZERO,
    add_stop_risk,
    decimal_str,
    fees_total,
    gross_pnl_usd,
    money_str,
    net_pnl_usd,
    outcome_tags,
    plain_money_str,
    quantize_money,
    r_multiple,
    return_percent,
    weighted_average_entry,
)

_SCHEMA_VERSION = "1.0.0"
FULL_CLOSE = "FULL_CLOSE"
PARTIAL_CLOSE = "PARTIAL_CLOSE"
ADD = "ADD"
STOP_TOUCH_RULE = "STOP_TOUCH"
TARGET_TOUCH_RULE = "TARGET_TOUCH"
MANUAL_CLOSE_RULE = "MANUAL_CLOSE"
FUNDING_REASON = "funding accrual not in this slice"
_EIGHT_DP = Decimal("0.00000001")


class PositionError(Exception):
    """A position-management operation is invalid for the current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProtectiveExit:
    """One deterministic stop-touch decision for an open position.

    The decision id is not derived here: the caller owns the recorded mark and
    its as-of, so it derives the id once with
    :func:`management_decision_id` and passes it through the close path.
    """

    position_id: str
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    stop_price: Decimal
    mark_price: Decimal
    at: str
    rule: str = STOP_TOUCH_RULE


def management_decision_id(position_id: str, mark: Decimal, mark_as_of: str) -> str:
    """The one ``MGD`` id for a management decision on a recorded mark.

    Single source of truth: the persisted ``decisions`` row, the position's
    ``management_history`` entry, and the trade-log ``decision_id`` all use the
    value returned here.
    """
    return deterministic_id("MGD", f"{position_id}:{mark}:{mark_as_of}")


@dataclass(frozen=True)
class ManagementEvent:
    """One management timeline entry, the single source for every projection.

    The position payload's ``management_history`` and the append-only
    ``position_events`` audit row are both rendered from this value, so they
    cannot diverge.
    """

    decision_id: str
    action: str
    at: str
    order_id: str | None
    note: str

    def to_history_entry(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action,
            "at": self.at,
            "executed_order_id": self.order_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class TradeLogContext:
    """The service-owned facts a full close's ``trade-log`` row is rendered from.

    The broker commits the trade log inside its one transaction, so everything
    the journal needs beyond the price arithmetic (signal references,
    provenance, bar-backed excursions, quality reasons) is computed by the
    position service and handed to the broker on the intent. No field is
    invented here: absent data is named in ``data_quality_reasons``.
    """

    signal: Mapping[str, Any]
    provenance: Mapping[str, Any]
    decision_quality: Mapping[str, Any]
    mae_usd: Decimal
    mfe_usd: Decimal
    risk_amount_usd: Decimal
    entry_notional_usd: Decimal
    data_quality_status: str
    data_quality_reasons: tuple[str, ...]
    close_reason: str = STOP_TOUCH_RULE
    stopped: bool = False
    target: bool = False
    manual: bool = False
    had_partial_close: bool = False
    traded_quantity: Decimal | None = None


@dataclass(frozen=True)
class PartialCloseResult:
    """One reduce-only slice applied to an OPEN position.

    The position stays OPEN and no trade log is created. The returned payload is
    the contract document the row will carry; the accumulated gross realized PnL
    is persisted in ``realized_pnl_usd`` so a later full close can total it.
    """

    payload: dict[str, Any]
    gross_pnl_usd: Decimal
    fees_usd: Decimal
    accumulated_gross_pnl_usd: Decimal
    event: ManagementEvent


@dataclass(frozen=True)
class AddResult:
    """One position-increasing fill applied in place; no trade log is created."""

    position: Position
    payload: dict[str, Any]
    event: ManagementEvent


@dataclass(frozen=True)
class CloseResult:
    """The closed position, its exact PnL terms, and the management event."""

    position: Position
    gross_pnl_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    net_pnl_usd: Decimal
    event: ManagementEvent

    def __post_init__(self) -> None:
        expected = quantize_money(self.gross_pnl_usd - self.fees_usd + self.funding_usd)
        if self.net_pnl_usd != expected:
            raise ValueError("close net PnL must equal gross - fees + funding")

    def to_contract(self) -> dict[str, Any]:
        return self.position.to_contract()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def position_from_contract(payload: Mapping[str, Any]) -> Position:
    """Build the pure :class:`Position` from its persisted contract payload.

    An OPEN position may carry accumulated realized PnL from earlier partial
    closes in ``realized_pnl_usd`` (the contract allows it); the pure
    :class:`Position` keeps that field close-only, so an OPEN payload maps it to
    ``None`` and the accounting is read with
    :func:`accumulated_realized_gross`.
    """
    state = str(payload.get("state"))
    realized = (
        None if state == PositionState.OPEN else _decimal_or_none(payload.get("realized_pnl_usd"))
    )
    try:
        position = Position(
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
            realized_pnl_usd=realized,
            funding_usd=_decimal(payload.get("funding_usd", "0")),
            management_history=tuple(payload.get("management_history") or ()),
            version=int(payload.get("version", 1)),
        )
        # Bound the structurally load-bearing magnitudes so a corrupt-but-finite
        # stored value cannot overflow a later ``Decimal.quantize`` (a 500) in
        # the financing/close math. The mark is deliberately not checked here:
        # the mark resolver ignores an implausible stored mark and prefers bars.
        if not position.size.is_finite() or not (0 < position.size <= MAX_ABS_QUANTITY):
            raise ValueError("the position size is outside the plausible range")
        if not position.avg_entry_price.is_finite() or not (
            0 < position.avg_entry_price <= MAX_ABS_PRICE
        ):
            raise ValueError("the average entry price is outside the plausible range")
        if not position.notional_usd.is_finite() or abs(position.notional_usd) > MAX_ABS_MONEY:
            raise ValueError("the position notional is outside the plausible range")
        return position
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


def accumulated_realized_gross(payload: Mapping[str, Any]) -> Decimal:
    """The gross PnL already realized by partial closes, from an OPEN payload.

    A corrupt or implausible persisted magnitude is treated as unmeasured
    (``ZERO``) rather than flowing into the money math.
    """
    raw = payload.get("realized_pnl_usd")
    if raw is None:
        return ZERO
    try:
        value = Decimal(str(raw))
    except ArithmeticError:
        return ZERO
    if not value.is_finite() or abs(value) > MAX_ABS_MONEY:
        return ZERO
    return value


def _require_plausible_money(value: Decimal, field: str) -> None:
    if not value.is_finite() or abs(value) > MAX_ABS_MONEY:
        raise PositionError(
            "TRADE_LOG_INVALID", f"the journal {field} is outside the plausible range"
        )


def _increasing_side(side: PositionSide) -> OrderSide:
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def _reducing_side(side: PositionSide) -> OrderSide:
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def partial_close(
    position: Position,
    fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal = ZERO,
    note: str = "partial close",
) -> PartialCloseResult:
    """Apply a reduce-only slice; the position stays OPEN and no trade log exists.

    The slice's gross PnL is added to the accumulated gross realized PnL, which
    is persisted in ``realized_pnl_usd`` so the eventual full close can total the
    whole trade. The exit fee joins the position's accumulated fees.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be reduced")
    if fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the close fill symbol does not match")
    if fill.side is not _reducing_side(position.side):
        raise PositionError("POSITION_STATE_INVALID", "the close fill side is wrong")
    if fill.quantity <= 0 or fill.quantity >= position.size:
        raise PositionError(
            "POSITION_STATE_INVALID",
            "a partial close must leave a positive size; use a full close for the remainder",
        )
    try:
        gross = gross_pnl_usd(
            position.side.value,
            position.avg_entry_price,
            fill.price,
            fill.quantity,
        )
        total_gross = quantize_money(Decimal(accumulated_gross) + gross)
        fees = fees_total(position.fees_usd, fill.fee)
        new_size = position.size - fill.quantity
        # Notional and margin track the remaining exposure; the frozen initial
        # risk stays untouched so R is measured against the risk the trade
        # actually took.
        remaining_ratio = new_size / position.size
        notional = quantize_money(position.notional_usd * remaining_ratio)
        margin = quantize_money(position.margin_usd * remaining_ratio)
    except (ArithmeticError, ValueError) as exc:
        raise PositionError(
            "DATA_UNAVAILABLE",
            "the recorded values make the partial-close money math implausible",
        ) from exc
    event = ManagementEvent(
        decision_id=decision_id,
        action=PARTIAL_CLOSE,
        at=at,
        order_id=fill.order_id,
        note=note,
    )
    payload = position.to_contract()
    payload["size"] = decimal_str(new_size)
    payload["notional_usd"] = plain_money_str(notional)
    payload["margin_usd"] = plain_money_str(margin)
    payload["fees_usd"] = plain_money_str(fees)
    payload["realized_pnl_usd"] = plain_money_str(total_gross)
    payload["mark_price"] = decimal_str(fill.price)
    payload["updated_at"] = at
    payload["management_history"] = [
        *(payload.get("management_history") or []),
        event.to_history_entry(),
    ]
    payload["version"] = int(payload.get("version", 1)) + 1
    return PartialCloseResult(
        payload=payload,
        gross_pnl_usd=gross,
        fees_usd=fees,
        accumulated_gross_pnl_usd=total_gross,
        event=event,
    )


def apply_add(
    position: Position,
    fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal = ZERO,
    note: str = "add to position",
) -> AddResult:
    """Apply a position-increasing fill in place.

    Size and weighted-average entry move deterministically, the frozen initial
    risk grows by the add's stop-based risk, and the position stays OPEN. A
    second position row is never created. ``accumulated_gross`` is the gross PnL
    already realized by earlier partial closes; it is written back because an
    add rebuilds the payload and must not erase that accounting.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be added to")
    if fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the add fill symbol does not match")
    if fill.side is not _increasing_side(position.side):
        raise PositionError("POSITION_STATE_INVALID", "the add fill side is wrong")
    stop = position.stop_price
    if stop is None or stop <= 0:
        raise PositionError("POSITION_STATE_INVALID", "an add requires a protective stop")
    validate_stop_side(position.side, fill.price, stop)
    try:
        risk_delta = add_stop_risk(fill.price, stop, fill.quantity)
        new_size = position.size + fill.quantity
        avg_entry = weighted_average_entry(
            position.avg_entry_price,
            position.size,
            fill.price,
            fill.quantity,
        )
        fees = fees_total(position.fees_usd, fill.fee)
        notional = quantize_money(position.notional_usd + fill.quote_quantity)
        margin = quantize_money(notional / Decimal(str(position.leverage)))
        updated_risk = quantize_money(position.risk_amount_usd + risk_delta)
        accumulated = quantize_money(Decimal(accumulated_gross))
    except (ArithmeticError, ValueError) as exc:
        raise PositionError(
            "DATA_UNAVAILABLE",
            "the recorded values make the add money math implausible",
        ) from exc
    event = ManagementEvent(
        decision_id=decision_id,
        action=ADD,
        at=at,
        order_id=fill.order_id,
        note=note,
    )
    updated = replace(
        position,
        size=new_size,
        avg_entry_price=avg_entry,
        mark_price=fill.price,
        notional_usd=notional,
        margin_usd=margin,
        risk_amount_usd=updated_risk,
        fees_usd=fees,
        updated_at=at,
        management_history=(*position.management_history, event.to_history_entry()),
        version=position.version + 1,
    )
    payload = updated.to_contract()
    # The pure ``Position`` carries realized PnL only when CLOSED, so an add
    # must copy the accumulated OPEN gross back onto the payload explicitly.
    payload["realized_pnl_usd"] = plain_money_str(accumulated)
    return AddResult(position=updated, payload=payload, event=event)


def close_full(
    position: Position,
    exit_fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal = ZERO,
    close_reason: str = STOP_TOUCH_RULE,
    note: str | None = None,
) -> CloseResult:
    """Apply a full-close fill to an OPEN position and return the closed result.

    The existing row is updated in place by the caller; this function never
    creates a second position. The exit fee is added to the entry fee already
    carried by the position, and any gross PnL realized by earlier partial
    closes (``accumulated_gross``) is included in the trade's gross total.
    ``decision_id`` is the caller-owned, single-sourced management id: it is
    written into the payload history and returned on the event so the broker's
    audit row cannot diverge.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be closed")
    if exit_fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill symbol does not match")
    if exit_fill.quantity != position.size:
        raise PositionError("POSITION_STATE_INVALID", "only a full close is supported")
    expected_side = _reducing_side(position.side)
    if exit_fill.side is not expected_side:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill side is wrong")

    try:
        slice_gross = gross_pnl_usd(
            position.side.value,
            position.avg_entry_price,
            exit_fill.price,
            position.size,
        )
        gross = quantize_money(Decimal(accumulated_gross) + slice_gross)
        fees = fees_total(position.fees_usd, exit_fill.fee)
        funding = position.funding_usd
        net = net_pnl_usd(gross, fees, funding)
    except (ArithmeticError, ValueError) as exc:
        raise PositionError(
            "DATA_UNAVAILABLE",
            "the recorded values make the full-close money math implausible",
        ) from exc
    event = ManagementEvent(
        decision_id=decision_id,
        action=FULL_CLOSE,
        at=at,
        order_id=exit_fill.order_id,
        note=note if note is not None else _close_note(close_reason),
    )
    history = (*position.management_history, event.to_history_entry())
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
        event=event,
    )


def _close_note(close_reason: str) -> str:
    if close_reason == TARGET_TOUCH_RULE:
        return "target touched"
    if close_reason == MANUAL_CLOSE_RULE:
        return "manual close"
    return "protective stop touched"


def trade_id_for(position_id: str) -> str:
    """The one deterministic trade-log id for a position."""
    return deterministic_id("TRD", position_id)


def validate_trade_log_context(
    position: Position,
    context: TradeLogContext,
    *,
    expected_entry_notional_usd: Decimal | None = None,
) -> None:
    """Reject a service-supplied journal context that contradicts the position.

    The broker is an execution boundary shared by future callers, so it must
    not persist an arbitrary R/return/quality/provenance verbatim. Every
    cross-check here is computable from the position the close is applied to,
    or from the recorded entry fill (when the caller supplies the derived
    entry notional).
    """
    if (
        not context.risk_amount_usd.is_finite()
        or context.risk_amount_usd != position.risk_amount_usd
    ):
        raise PositionError(
            "TRADE_LOG_INVALID", "the trade-log risk does not match the position's frozen risk"
        )
    _require_plausible_money(context.entry_notional_usd, "entry notional")
    if context.entry_notional_usd <= 0:
        raise PositionError("TRADE_LOG_INVALID", "the trade-log entry notional must be positive")
    if expected_entry_notional_usd is not None:
        # An immutable entry notional resolved from the recorded entry fill is
        # the authoritative basis; a partial close must not shrink it.
        if context.entry_notional_usd != expected_entry_notional_usd:
            raise PositionError(
                "TRADE_LOG_INVALID",
                "the trade-log entry notional does not match the recorded entry fill",
            )
    elif context.entry_notional_usd < position.notional_usd:
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log entry notional is below the position's current notional",
        )
    _require_plausible_money(context.mae_usd, "MAE")
    _require_plausible_money(context.mfe_usd, "MFE")
    if context.mae_usd > ZERO or context.mfe_usd < ZERO:
        raise PositionError("TRADE_LOG_INVALID", "MAE must be <= 0 and MFE must be >= 0")
    if context.traded_quantity is not None and (
        not context.traded_quantity.is_finite()
        or context.traded_quantity <= 0
        or context.traded_quantity > MAX_ABS_QUANTITY
        or context.traded_quantity < position.size
    ):
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log traded quantity must cover the open size and be plausible",
        )
    if context.data_quality_status not in {"COMPLETE", "PARTIAL"}:
        raise PositionError(
            "TRADE_LOG_INVALID", f"unknown data-quality status {context.data_quality_status!r}"
        )
    if context.data_quality_status == "PARTIAL" and not context.data_quality_reasons:
        raise PositionError(
            "TRADE_LOG_INVALID", "a PARTIAL trade log must name at least one reason"
        )
    outcome_flags = sum((context.stopped, context.target, context.manual))
    if outcome_flags > 1:
        raise PositionError("TRADE_LOG_INVALID", "at most one of stopped/target/manual may be set")
    if context.manual and context.close_reason != MANUAL_CLOSE_RULE:
        raise PositionError(
            "TRADE_LOG_INVALID", "a manual close must carry the manual close reason"
        )
    if context.stopped and context.close_reason in {TARGET_TOUCH_RULE, MANUAL_CLOSE_RULE}:
        raise PositionError(
            "TRADE_LOG_INVALID", "a stopped outcome contradicts the recorded close reason"
        )
    if context.target and context.close_reason in {STOP_TOUCH_RULE, MANUAL_CLOSE_RULE}:
        raise PositionError(
            "TRADE_LOG_INVALID", "a target outcome contradicts the recorded close reason"
        )


def trade_log_from_close(
    close: CloseResult,
    *,
    trade_id: str,
    context: TradeLogContext | None = None,
    expected_entry_notional_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Render the closed trade as a ``trade-log.schema.json`` document.

    Without a ``context`` the legacy slice-23 rendering is preserved (funding is
    the close's derived term and every unmeasured field is named). With a
    context the complete journal document is rendered: signal references,
    provenance, outcome tags, data quality, MAE/MFE, R multiple, and return.
    """
    position = close.position
    entry_price = position.avg_entry_price
    exit_price = position.exit_price
    if exit_price is None:  # pragma: no cover - close_full always sets it
        raise PositionError("POSITION_STATE_INVALID", "the closed position has no exit price")
    base: dict[str, Any] = {
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
    }
    if context is None:
        return _legacy_trade_log(base, close, position)
    validate_trade_log_context(
        position, context, expected_entry_notional_usd=expected_entry_notional_usd
    )
    return _complete_trade_log(base, close, position, context)


def _legacy_trade_log(
    base: dict[str, Any],
    close: CloseResult,
    position: Position,
) -> dict[str, Any]:
    notional = position.avg_entry_price * position.size
    if notional > 0:
        computed_return = float(quantize_money(close.net_pnl_usd / notional * Decimal("100")))
    else:  # pragma: no cover - a position size is always positive
        computed_return = 0.0
    return {
        **base,
        "r_multiple": None,
        "mae_usd": "0",
        "mfe_usd": "0",
        "return_pct": computed_return,
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


def _complete_trade_log(
    base: dict[str, Any],
    close: CloseResult,
    position: Position,
    context: TradeLogContext,
) -> dict[str, Any]:
    computed_r = r_multiple(close.net_pnl_usd, context.risk_amount_usd)
    computed_return = return_percent(close.net_pnl_usd, context.entry_notional_usd)
    reasons = list(context.data_quality_reasons)
    if computed_r is None and not any("R multiple" in reason for reason in reasons):
        reasons.append("R multiple not measured: the frozen initial risk is zero or missing")
    tags = outcome_tags(
        close.net_pnl_usd,
        stopped=context.stopped,
        target=context.target,
        manual=context.manual,
        partial=context.had_partial_close,
        data_partial=context.data_quality_status == "PARTIAL",
    )
    base["size"] = decimal_str(
        position.size if context.traded_quantity is None else context.traded_quantity
    )
    return {
        **base,
        "r_multiple": computed_r,
        "mae_usd": money_str(context.mae_usd),
        "mfe_usd": money_str(context.mfe_usd),
        "return_pct": computed_return,
        "signal": dict(context.signal),
        "decision_quality": dict(context.decision_quality),
        "outcome_tags": list(tags),
        "review_decision_id": None,
        "data_quality": {
            "status": context.data_quality_status,
            "reasons": reasons,
            "gaps": [],
            "last_event_at": position.closed_at,
            "coverage_start": position.opened_at,
        },
        "provenance": dict(context.provenance),
    }


__all__ = [
    "ADD",
    "FULL_CLOSE",
    "FUNDING_REASON",
    "MANUAL_CLOSE_RULE",
    "PARTIAL_CLOSE",
    "STOP_TOUCH_RULE",
    "TARGET_TOUCH_RULE",
    "AddResult",
    "CloseResult",
    "ManagementEvent",
    "PartialCloseResult",
    "PositionError",
    "ProtectiveExit",
    "TradeLogContext",
    "accumulated_realized_gross",
    "apply_add",
    "close_full",
    "evaluate_protective_exit",
    "management_decision_id",
    "partial_close",
    "position_from_contract",
    "trade_id_for",
    "trade_log_from_close",
    "validate_stop_side",
    "validate_trade_log_context",
]
