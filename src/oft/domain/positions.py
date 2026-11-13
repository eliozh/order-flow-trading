"""Pure position-management domain: partial close, add, full close, and the journal.

Deterministic and I/O-free. The module reads a :class:`~oft.domain.oms.Position`
(or its contract payload), applies the code-owned rules (a recorded mark that
touches the protective stop, a reduce-only slice, an add, and the eventual full
close), and produces the updated position payload plus the trade-log document.
The PnL bases are explicit: an OPEN position's ``realized_pnl_usd`` is the
ACCUMULATED GROSS realized by partial closes, while a CLOSED position's value is
the NET result. Identifiers are deterministic functions of their inputs, so a
replay with the same fixed clock reproduces the same ids and values.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
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
FUNDING_REASON = "funding is not itemised per settlement in this legacy journal rendering"
_EIGHT_DP = Decimal("0.00000001")


class CloseOutcome(StrEnum):
    """The one authority for a close's outcome and the projections it drives.

    The flags on :class:`TradeLogContext` and the ``outcome_tags`` are derived
    from this value rather than chosen independently, so a target close cannot
    be tagged ``STOPPED`` and an unknown recorded reason is rejected instead of
    being relabelled "protective stop touched".
    """

    STOP_TOUCH = STOP_TOUCH_RULE
    TARGET_TOUCH = TARGET_TOUCH_RULE
    MANUAL_CLOSE = MANUAL_CLOSE_RULE

    @property
    def note(self) -> str:
        if self is CloseOutcome.TARGET_TOUCH:
            return "target touched"
        if self is CloseOutcome.MANUAL_CLOSE:
            return "manual close"
        return "protective stop touched"

    @property
    def stopped(self) -> bool:
        return self is CloseOutcome.STOP_TOUCH

    @property
    def target(self) -> bool:
        return self is CloseOutcome.TARGET_TOUCH

    @property
    def manual(self) -> bool:
        return self is CloseOutcome.MANUAL_CLOSE

    @classmethod
    def from_reason(cls, reason: str) -> CloseOutcome:
        """Parse a recorded close reason, failing closed on an unknown value."""
        try:
            return cls(reason)
        except ValueError as exc:
            raise PositionError("TRADE_LOG_INVALID", f"unknown close reason {reason!r}") from exc


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

    ``entry_notional_usd`` is the immutable entry fill's quote quantity. It is
    ``None`` when that fill could not be resolved; the residual position
    notional is never substituted for it, and the journal then marks
    ``return_pct`` unmeasured. ``close_reason`` is the outcome authority: the
    ``stopped``/``target``/``manual`` flags must agree with it.
    """

    signal: Mapping[str, Any]
    provenance: Mapping[str, Any]
    decision_quality: Mapping[str, Any]
    mae_usd: Decimal
    mfe_usd: Decimal
    risk_amount_usd: Decimal
    entry_notional_usd: Decimal | None
    data_quality_status: str
    data_quality_reasons: tuple[str, ...]
    close_reason: str = STOP_TOUCH_RULE
    stopped: bool = True
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
    ``slice_gross_pnl_usd`` is this slice's gross, ``accumulated_fees_usd`` is
    the entry fee plus every exit fee so far, and ``accumulated_gross_pnl_usd``
    is the running gross of all slices. The payload is defensively copied so no
    two results share one mutable document.
    """

    payload: dict[str, Any]
    slice_gross_pnl_usd: Decimal
    accumulated_fees_usd: Decimal
    accumulated_gross_pnl_usd: Decimal
    event: ManagementEvent

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))


@dataclass(frozen=True)
class AddResult:
    """One position-increasing fill applied in place; no trade log is created.

    ``position`` is the pure OPEN position (whose ``realized_pnl_usd`` is
    intentionally ``None``); ``accumulated_gross_pnl_usd`` carries the gross a
    partial close already realized, which is written only to the contract
    payload. The payload is defensively copied so no two results share one
    mutable document.
    """

    position: Position
    payload: dict[str, Any]
    accumulated_gross_pnl_usd: Decimal
    event: ManagementEvent

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))


@dataclass(frozen=True)
class CloseResult:
    """The closed position, its exact PnL terms, the outcome, and the event.

    ``outcome`` is the authority for the journal's flags and tags: the applied
    close decides STOP/TARGET/MANUAL, and a service-supplied context must agree
    with it.
    """

    position: Position
    gross_pnl_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    net_pnl_usd: Decimal
    outcome: CloseOutcome
    event: ManagementEvent

    def __post_init__(self) -> None:
        expected = quantize_money(self.gross_pnl_usd - self.fees_usd + self.funding_usd)
        if self.net_pnl_usd != expected:
            raise ValueError("close net PnL must equal gross - fees + funding")
        if self.event.action != FULL_CLOSE:
            raise ValueError("a close result must carry a FULL_CLOSE event")

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
        # A persisted history entry must be an object; a bare string would later
        # raise AttributeError in the outcome/partial derivation.
        if any(not isinstance(entry, Mapping) for entry in position.management_history):
            raise ValueError("a position management history entry is not an object")
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


def _partial_close_present(history: object) -> bool:
    """Whether a stored management history contains a PARTIAL_CLOSE event.

    A history element that is not an object is corrupt and fails closed with a
    typed error instead of raising ``AttributeError`` on ``.get``.
    """
    if not isinstance(history, (list, tuple)):
        raise PositionError("DATA_UNAVAILABLE", "the position management history is corrupt")
    for entry in history:
        if not isinstance(entry, Mapping):
            raise PositionError("DATA_UNAVAILABLE", "the position management history is corrupt")
        if entry.get("action") == PARTIAL_CLOSE:
            return True
    return False


def recorded_partial_close(position: Position) -> bool:
    """Whether the position's recorded timeline contains a PARTIAL_CLOSE event."""
    return _partial_close_present(position.management_history)


def payload_has_partial_close(payload: Mapping[str, Any]) -> bool:
    """Whether a persisted position payload's history records a partial close."""
    return _partial_close_present(payload.get("management_history") or ())


def accumulated_realized_gross(payload: Mapping[str, Any]) -> Decimal | None:
    """The gross PnL already realized by partial closes, from an OPEN payload.

    ``None`` means *unmeasured*: the field is present but corrupt, non-finite, or
    outside the plausible money range, or the field is absent while the recorded
    history shows a partial close (the accumulator was lost). Callers must fail
    closed with a named typed error instead of treating an unreadable
    accumulator as ``ZERO`` and persisting that zero over the earlier partial
    PnL. An absent field with no recorded partial close is a measured zero.
    """
    raw = payload.get("realized_pnl_usd")
    if raw is None:
        return None if payload_has_partial_close(payload) else ZERO
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not value.is_finite() or abs(value) > MAX_ABS_MONEY:
        return None
    return value


def _require_plausible_money(value: Decimal, field: str) -> None:
    if not value.is_finite() or abs(value) > MAX_ABS_MONEY:
        raise PositionError(
            "TRADE_LOG_INVALID", f"the journal {field} is outside the plausible range"
        )


def increasing_side(side: PositionSide) -> OrderSide:
    """The order side that increases a position of ``side``."""
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def reducing_side(side: PositionSide) -> OrderSide:
    """The order side that reduces (exits) a position of ``side``."""
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def partial_close(
    position: Position,
    fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal,
    note: str = "partial close",
) -> PartialCloseResult:
    """Apply a reduce-only slice; the position stays OPEN and no trade log exists.

    The slice's gross PnL is added to ``accumulated_gross`` (the gross already
    realized by earlier partial closes, which the caller must supply -- it has
    no safe default because a missing accumulator would erase prior accounting)
    and persisted in ``realized_pnl_usd``. On an OPEN position that contract
    field is the ACCUMULATED GROSS, not a net result. The exit fee joins the
    position's accumulated fees.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be reduced")
    if fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the close fill symbol does not match")
    if fill.side is not reducing_side(position.side):
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
    # OPEN-position contract semantics: realized_pnl_usd is the ACCUMULATED
    # GROSS realized so far, never a net result. A later close totals it.
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
        slice_gross_pnl_usd=gross,
        accumulated_fees_usd=fees,
        accumulated_gross_pnl_usd=total_gross,
        event=event,
    )


def apply_add(
    position: Position,
    fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal,
    note: str = "add to position",
) -> AddResult:
    """Apply a position-increasing fill in place.

    Size and weighted-average entry move deterministically, the frozen initial
    risk grows by the add's stop-based risk, and the position stays OPEN. A
    second position row is never created. ``accumulated_gross`` is the gross PnL
    already realized by earlier partial closes; it is required because an add
    rebuilds the payload and must not erase that accounting, and it is returned
    as a typed field so the pure ``position`` and the contract payload cannot
    silently diverge.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be added to")
    if fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the add fill symbol does not match")
    if fill.side is not increasing_side(position.side):
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
    # On an OPEN payload that field means ACCUMULATED GROSS.
    payload["realized_pnl_usd"] = plain_money_str(accumulated)
    return AddResult(
        position=updated,
        payload=payload,
        accumulated_gross_pnl_usd=accumulated,
        event=event,
    )


def close_full(
    position: Position,
    exit_fill: Fill,
    *,
    at: str,
    decision_id: str,
    accumulated_gross: Decimal,
    close_reason: str = STOP_TOUCH_RULE,
    note: str | None = None,
) -> CloseResult:
    """Apply a full-close fill to an OPEN position and return the closed result.

    The existing row is updated in place by the caller; this function never
    creates a second position. The exit fee is added to the entry fee already
    carried by the position, and any gross PnL realized by earlier partial
    closes (``accumulated_gross``, required -- an absent accumulator must never
    be assumed zero) is included in the trade's gross total. On the CLOSED
    payload ``realized_pnl_usd`` becomes the NET result. ``decision_id`` is the
    caller-owned, single-sourced management id: it is written into the payload
    history and returned on the event so the broker's audit row cannot diverge.
    """
    if position.state != PositionState.OPEN:
        raise PositionError("POSITION_STATE_INVALID", "only an OPEN position can be closed")
    if exit_fill.symbol != position.symbol:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill symbol does not match")
    if exit_fill.quantity != position.size:
        raise PositionError("POSITION_STATE_INVALID", "only a full close is supported")
    expected_side = reducing_side(position.side)
    if exit_fill.side is not expected_side:
        raise PositionError("POSITION_STATE_INVALID", "the exit fill side is wrong")
    outcome = CloseOutcome.from_reason(close_reason)

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
        note=note if note is not None else outcome.note,
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
        outcome=outcome,
        event=event,
    )


def trade_id_for(position_id: str) -> str:
    """The one deterministic trade-log id for a position."""
    return deterministic_id("TRD", position_id)


def validate_trade_log_context(
    position: Position,
    context: TradeLogContext,
    *,
    expected_entry_notional_usd: Decimal | None = None,
    expected_traded_quantity: Decimal | None = None,
    actual_outcome: CloseOutcome | None = None,
) -> None:
    """Reject a service-supplied journal context that contradicts the position.

    The broker is an execution boundary shared by future callers, so it must
    not persist an arbitrary R/return/quality/provenance/outcome verbatim.
    ``expected_traded_quantity`` and ``actual_outcome`` are derived from the
    recorded events and the applied :class:`CloseResult`; when supplied they are
    authoritative and a mismatch fails closed. ``entry_notional_usd`` may be
    ``None`` only when the journal already names the gap and marks itself
    PARTIAL; the residual position notional is never accepted as a substitute.
    """
    if (
        not context.risk_amount_usd.is_finite()
        or context.risk_amount_usd != position.risk_amount_usd
    ):
        raise PositionError(
            "TRADE_LOG_INVALID", "the trade-log risk does not match the position's frozen risk"
        )
    if context.entry_notional_usd is None:
        if context.data_quality_status != "PARTIAL" or not context.data_quality_reasons:
            raise PositionError(
                "TRADE_LOG_INVALID",
                "an unmeasured entry notional requires a named PARTIAL reason",
            )
    else:
        _require_plausible_money(context.entry_notional_usd, "entry notional")
        if context.entry_notional_usd <= 0:
            raise PositionError(
                "TRADE_LOG_INVALID", "the trade-log entry notional must be positive"
            )
        if expected_entry_notional_usd is not None:
            # An immutable entry notional resolved from the recorded entry fill
            # is the authoritative basis; a partial close must not shrink it.
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
    # The partial-close flag and the traded quantity are derived from the
    # recorded PARTIAL_CLOSE events; a caller-supplied value cannot disagree and
    # cannot be silently omitted.
    recorded_partial = recorded_partial_close(position)
    if context.traded_quantity is None:
        raise PositionError("TRADE_LOG_INVALID", "the trade-log traded quantity is required")
    if (
        not context.traded_quantity.is_finite()
        or context.traded_quantity <= 0
        or context.traded_quantity > MAX_ABS_QUANTITY
        or context.traded_quantity < position.size
    ):
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log traded quantity must cover the open size and be plausible",
        )
    if expected_traded_quantity is not None:
        if context.traded_quantity != expected_traded_quantity:
            raise PositionError(
                "TRADE_LOG_INVALID",
                "the trade-log traded quantity does not match the recorded partial closes",
            )
        if recorded_partial and context.traded_quantity <= position.size:
            raise PositionError(
                "TRADE_LOG_INVALID",
                "a trade with a recorded partial close must trade more than the open size",
            )
    elif recorded_partial and context.traded_quantity <= position.size:
        raise PositionError(
            "TRADE_LOG_INVALID",
            "a trade with a recorded partial close must trade more than the open size",
        )
    if context.had_partial_close != recorded_partial:
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log partial-close flag contradicts the recorded partial closes",
        )
    if not recorded_partial and context.traded_quantity != position.size:
        raise PositionError(
            "TRADE_LOG_INVALID",
            "a trade without a recorded partial close must trade only the open size",
        )
    if context.data_quality_status not in {"COMPLETE", "PARTIAL"}:
        raise PositionError(
            "TRADE_LOG_INVALID", f"unknown data-quality status {context.data_quality_status!r}"
        )
    if context.data_quality_status == "PARTIAL" and not context.data_quality_reasons:
        raise PositionError(
            "TRADE_LOG_INVALID", "a PARTIAL trade log must name at least one reason"
        )
    outcome = CloseOutcome.from_reason(context.close_reason)
    if actual_outcome is not None and actual_outcome is not outcome:
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log outcome contradicts the applied close outcome",
        )
    if (
        context.stopped != outcome.stopped
        or context.target != outcome.target
        or context.manual != outcome.manual
    ):
        raise PositionError(
            "TRADE_LOG_INVALID",
            "the trade-log outcome flags contradict the close outcome",
        )


def trade_log_from_close(
    close: CloseResult,
    *,
    trade_id: str,
    context: TradeLogContext | None = None,
    expected_entry_notional_usd: Decimal | None = None,
    expected_traded_quantity: Decimal | None = None,
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
        position,
        context,
        expected_entry_notional_usd=expected_entry_notional_usd,
        expected_traded_quantity=expected_traded_quantity,
        actual_outcome=close.outcome,
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
    if context.entry_notional_usd is None and not any(
        "entry notional" in reason for reason in reasons
    ):
        reasons.append("entry notional is unavailable: return_pct is not measured")
    # ``return_percent`` legitimately rounds a tiny measured return to ``0.0``;
    # that is a measured zero, not an unmeasured gap, so no reason is added.
    # The status is derived from the final reasons, so a ratio that degraded
    # inside this renderer can never leave a COMPLETE journal with a stated gap.
    status = "PARTIAL" if reasons else "COMPLETE"
    tags = outcome_tags(
        close.net_pnl_usd,
        stopped=close.outcome.stopped,
        target=close.outcome.target,
        manual=close.outcome.manual,
        partial=context.had_partial_close,
        data_partial=status == "PARTIAL",
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
            "status": status,
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
    "CloseOutcome",
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
    "increasing_side",
    "management_decision_id",
    "partial_close",
    "payload_has_partial_close",
    "position_from_contract",
    "recorded_partial_close",
    "reducing_side",
    "trade_id_for",
    "trade_log_from_close",
    "validate_stop_side",
    "validate_trade_log_context",
]
