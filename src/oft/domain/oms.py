"""Order, fill, and position state machines for the single execution path.

The module is pure: identifiers are deterministic functions of their inputs and
state transitions are validated against the ADR-0006 machine. No I/O, clock, or
provider type enters here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

from oft.domain.pnl import plain_money_str

_SCHEMA_VERSION = "1.0.0"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"
    RECONCILING = "RECONCILING"


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    }
)

_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.UNKNOWN,
        }
    ),
}


class OrderStateError(Exception):
    """A transition is not part of the documented order state machine."""

    code = "POSITION_STATE_INVALID"


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def intent_hash(payload: Mapping[str, Any]) -> str:
    """Hash a normalized intent document so the same intent always matches."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def client_order_id(environment: str, symbol: str, purpose: str, intent_hash_value: str) -> str:
    """``oft-{env}-{symbol}-{purpose}-{hash[:16]}`` (ADR-0006)."""
    digest = hashlib.sha256(intent_hash_value.encode("utf-8")).hexdigest()[:16]
    return f"oft-{environment.lower()}-{symbol}-{purpose}-{digest}"[:64]


def deterministic_id(prefix: str, seed: str) -> str:
    """Stable record id derived from a seed, never from random state."""
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16].upper()}"


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    environment: str
    account_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    fee: Decimal
    fee_asset: str
    maker: bool
    filled_at: str
    received_at: str
    source: str = "PAPER"
    exchange_trade_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.price <= 0:
            raise ValueError("fill price must be positive")

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "exchange_trade_id": self.exchange_trade_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "quote_quantity": str(self.quote_quantity),
            "fee": str(self.fee),
            "fee_asset": self.fee_asset,
            "maker": self.maker,
            "filled_at": self.filled_at,
            "received_at": self.received_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class Position:
    position_id: str
    account_id: str
    environment: str
    symbol: str
    side: PositionSide
    state: str
    size: Decimal
    entry_price: Decimal
    avg_entry_price: Decimal
    mark_price: Decimal | None
    notional_usd: Decimal
    stop_price: Decimal | None
    target_prices: tuple[Decimal, ...]
    leverage: float
    margin_usd: Decimal
    risk_amount_usd: Decimal
    risk_pct: float
    fees_usd: Decimal
    opened_at: str
    updated_at: str
    source_trigger_id: str | None = None
    source_decision_id: str | None = None
    closed_at: str | None = None
    exit_price: Decimal | None = None
    realized_pnl_usd: Decimal | None = None
    funding_usd: Decimal = Decimal("0")
    management_history: tuple[dict[str, Any], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if self.state not in {PositionState.OPEN, PositionState.CLOSED}:
            raise ValueError("this slice only creates OPEN or CLOSED positions")
        if self.size <= 0:
            raise ValueError("position size must be positive")
        if self.state == PositionState.OPEN:
            if (
                self.closed_at is not None
                or self.exit_price is not None
                or self.realized_pnl_usd is not None
            ):
                raise ValueError("an OPEN position must not carry close fields")
        else:
            if self.closed_at is None or self.exit_price is None:
                raise ValueError("a CLOSED position requires closed_at and exit_price")
            if self.realized_pnl_usd is None:
                raise ValueError("a CLOSED position requires realized_pnl_usd")

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "position_id": self.position_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "symbol": self.symbol,
            "side": self.side.value,
            "state": self.state,
            "size": str(self.size),
            "entry_price": str(self.entry_price),
            "avg_entry_price": str(self.avg_entry_price),
            "mark_price": None if self.mark_price is None else str(self.mark_price),
            "notional_usd": str(self.notional_usd),
            "unrealized_pnl_usd": None,
            "unrealized_pnl_pct": None,
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "target_prices": [str(price) for price in self.target_prices],
            "leverage": self.leverage,
            "margin_mode": "ISOLATED",
            "margin_usd": str(self.margin_usd),
            "risk_amount_usd": str(self.risk_amount_usd),
            "risk_pct": self.risk_pct,
            "fees_usd": plain_money_str(self.fees_usd),
            "funding_usd": plain_money_str(self.funding_usd),
            "realized_pnl_usd": (
                None if self.realized_pnl_usd is None else plain_money_str(self.realized_pnl_usd)
            ),
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "exit_price": None if self.exit_price is None else str(self.exit_price),
            "source_trigger_id": self.source_trigger_id,
            "source_decision_id": self.source_decision_id,
            "management_history": [dict(entry) for entry in self.management_history],
            "data_quality": {
                "status": "COMPLETE",
                "reasons": [],
                "gaps": [],
                "last_event_at": self.updated_at,
                "coverage_start": None,
            },
            "version": self.version,
        }


@dataclass(frozen=True)
class Order:
    order_id: str
    client_order_id: str
    environment: str
    account_id: str
    position_id: str | None
    symbol: str
    side: OrderSide
    type: OrderType
    time_in_force: str
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    reduce_only: bool
    status: OrderStatus
    risk_verdict_id: str
    approval_id: str | None
    intent_hash: str
    idempotency_key: str
    created_at: str
    submitted_at: str | None
    updated_at: str
    filled_at: str | None = None
    avg_fill_price: Decimal | None = None
    filled_quantity: Decimal = Decimal("0")
    error_code: str | None = None
    error_message: str | None = None
    exchange_order_id: str | None = None
    trace: tuple[str, ...] = ()

    def transition(self, target: OrderStatus, *, at: str) -> Order:
        if self.status in _TERMINAL_STATUSES:
            raise OrderStateError(f"{self.status} is terminal")
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            raise OrderStateError(f"invalid transition {self.status} -> {target}")
        return replace(self, status=target, updated_at=at, trace=(*self.trace, target.value))

    @property
    def quote_quantity(self) -> Decimal | None:
        if self.avg_fill_price is None:
            return None
        return (self.avg_fill_price * self.filled_quantity).quantize(Decimal("0.00000001"))

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.type.value,
            "time_in_force": self.time_in_force,
            "quantity": str(self.quantity),
            "price": None if self.price is None else str(self.price),
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "reduce_only": self.reduce_only,
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "avg_fill_price": None if self.avg_fill_price is None else str(self.avg_fill_price),
            "quote_quantity": (None if self.quote_quantity is None else str(self.quote_quantity)),
            "risk_verdict_id": self.risk_verdict_id,
            "approval_id": self.approval_id,
            "intent_hash": self.intent_hash,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "filled_at": self.filled_at,
            "error": (
                None
                if self.error_code is None
                else {
                    "code": self.error_code,
                    "message": self.error_message or "",
                    "retryable": False,
                    "exchange_code": None,
                }
            ),
        }


def order_from_intent(
    *,
    order_id: str,
    client_order_id_value: str,
    environment: str,
    account_id: str,
    symbol: str,
    side: OrderSide,
    order_type: OrderType,
    quantity: Decimal,
    price: Decimal | None,
    stop_price: Decimal | None,
    risk_verdict_id: str,
    approval_id: str | None,
    intent_hash_value: str,
    idempotency_key: str,
    created_at: str,
    status: OrderStatus = OrderStatus.CREATED,
    position_id: str | None = None,
    reduce_only: bool = False,
) -> Order:
    return Order(
        order_id=order_id,
        client_order_id=client_order_id_value,
        environment=environment,
        account_id=account_id,
        position_id=position_id,
        symbol=symbol,
        side=side,
        type=order_type,
        time_in_force="GTC",
        quantity=quantity,
        price=price,
        stop_price=stop_price,
        reduce_only=reduce_only,
        status=status,
        risk_verdict_id=risk_verdict_id,
        approval_id=approval_id,
        intent_hash=intent_hash_value,
        idempotency_key=idempotency_key,
        created_at=created_at,
        submitted_at=None,
        updated_at=created_at,
    )


def position_from_fill(
    fill: Fill,
    *,
    position_id: str,
    side: PositionSide,
    stop_price: Decimal | None,
    target_prices: tuple[Decimal, ...],
    leverage: float,
    mark_price: Decimal,
    risk_amount_usd: Decimal,
    risk_pct: float,
    opened_at: str,
) -> Position:
    margin = (fill.quote_quantity / Decimal(str(leverage))).quantize(Decimal("0.00000001"))
    return Position(
        position_id=position_id,
        account_id=fill.account_id,
        environment=fill.environment,
        symbol=fill.symbol,
        side=side,
        state=PositionState.OPEN.value,
        size=fill.quantity,
        entry_price=fill.price,
        avg_entry_price=fill.price,
        mark_price=mark_price,
        notional_usd=fill.quote_quantity,
        stop_price=stop_price,
        target_prices=target_prices,
        leverage=leverage,
        margin_usd=margin,
        risk_amount_usd=risk_amount_usd,
        risk_pct=risk_pct,
        fees_usd=fill.fee,
        opened_at=opened_at,
        updated_at=opened_at,
    )
