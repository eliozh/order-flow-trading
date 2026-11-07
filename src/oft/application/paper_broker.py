"""Deterministic paper broker (ADR-0006).

Market fills at the mark/last price plus a configurable slippage (default
2 bps) and taker fee (default 5 bps). No queue modeling, no partial fills, no
randomness: the same fixed clock and inputs produce the same ids and values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from oft.config import Settings
from oft.domain.oms import (
    Fill,
    Order,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
    client_order_id,
    deterministic_id,
    intent_hash,
    order_from_intent,
    position_from_fill,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import (
    AuditEventDraft,
    FillRecord,
    OrderRecord,
    PaperAccountRecord,
    PositionRecord,
    StorePort,
)

_EIGHT_DP = Decimal("0.00000001")
_DEFAULT_ACCOUNT_ID = "ACC-PAPER"


class BrokerError(Exception):
    code = "ORDER_REJECTED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    take_profits: tuple[Decimal, ...]
    leverage: float
    decision_id: str
    risk_verdict_id: str
    approval_id: str
    idempotency_key: str
    position_side: PositionSide
    risk_amount_usd: Decimal
    risk_pct: float
    environment: str = "PAPER"
    purpose: str = "entry"
    account_id: str = _DEFAULT_ACCOUNT_ID

    def hash(self) -> str:
        return intent_hash(
            {
                "symbol": self.symbol,
                "side": self.side.value,
                "type": self.order_type.value,
                "quantity": str(self.quantity),
                "price": None if self.price is None else str(self.price),
                "stop_price": None if self.stop_price is None else str(self.stop_price),
                "take_profits": [str(price) for price in self.take_profits],
                "leverage": self.leverage,
                "decision_id": self.decision_id,
                "risk_verdict_id": self.risk_verdict_id,
                "environment": self.environment,
            }
        )


@dataclass(frozen=True)
class BrokerResult:
    order: Order
    fill: Fill
    position: Position
    account: PaperAccountRecord


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_EIGHT_DP, rounding=ROUND_DOWN)


class PaperBroker:
    def __init__(self, *, settings: Settings, store: StorePort, clock: ClockPort) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._slippage = Decimal(settings.paper_slippage_bps) / Decimal("10000")
        self._taker_fee = Decimal(settings.paper_taker_fee_bps) / Decimal("10000")
        self._initial_balance = Decimal(settings.paper_initial_balance)

    def account(self) -> PaperAccountRecord:
        existing = self._store.get_paper_account(_DEFAULT_ACCOUNT_ID)
        if existing is not None:
            return existing
        now = utc_timestamp(self._clock.now())
        return PaperAccountRecord(
            account_id=_DEFAULT_ACCOUNT_ID,
            environment="PAPER",
            currency="USDT",
            initial_balance=str(self._initial_balance),
            balance=str(self._initial_balance),
            updated_at=now,
        )

    def submit(
        self,
        intent: OrderIntent,
        *,
        mark_price: Decimal,
        mark_as_of: str,
        now: str,
        audit_actor: dict[str, Any] | None = None,
    ) -> BrokerResult:
        if intent.quantity <= 0:
            raise BrokerError("quantity must be positive")
        if mark_price <= 0:
            raise BrokerError("mark price must be positive")
        direction = Decimal("1") if intent.side is OrderSide.BUY else Decimal("-1")
        fill_price = _quantize(mark_price * (Decimal("1") + direction * self._slippage))
        quote_quantity = _quantize(fill_price * intent.quantity)
        fee = _quantize(quote_quantity * self._taker_fee)
        account = self.account()
        balance_before = Decimal(account.balance)
        balance_after = _quantize(balance_before - quote_quantity - fee)

        digest = intent.hash()
        client_id = client_order_id(intent.environment, intent.symbol, intent.purpose, digest)
        order_id = deterministic_id("ORD", f"{client_id}:order")
        fill_id = deterministic_id("FIL", f"{client_id}:fill")
        position_id = deterministic_id("POS", f"{client_id}:position")

        fill = Fill(
            fill_id=fill_id,
            order_id=order_id,
            environment=intent.environment,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            price=fill_price,
            quantity=intent.quantity,
            quote_quantity=quote_quantity,
            fee=fee,
            fee_asset="USDT",
            maker=False,
            filled_at=now,
            received_at=now,
            source="PAPER",
        )
        order = order_from_intent(
            order_id=order_id,
            client_order_id_value=client_id,
            environment=intent.environment,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            price=intent.price,
            stop_price=intent.stop_price,
            risk_verdict_id=intent.risk_verdict_id,
            approval_id=intent.approval_id,
            intent_hash_value=digest,
            idempotency_key=intent.idempotency_key,
            created_at=now,
            status=_filled_status(),
            position_id=position_id,
        )
        order = _complete_fill(order, fill, at=now)
        position = position_from_fill(
            fill,
            position_id=position_id,
            side=intent.position_side,
            stop_price=intent.stop_price,
            target_prices=intent.take_profits,
            leverage=intent.leverage,
            mark_price=mark_price,
            risk_amount_usd=intent.risk_amount_usd,
            risk_pct=intent.risk_pct,
            opened_at=now,
        )
        updated_account = PaperAccountRecord(
            account_id=account.account_id,
            environment=account.environment,
            currency=account.currency,
            initial_balance=account.initial_balance,
            balance=str(balance_after),
            updated_at=now,
        )
        self._store.save_order_fill_position(
            order=_order_record(order),
            fill=_fill_record(fill),
            position=_position_record(position),
            account=updated_account,
            audit=(
                None
                if audit_actor is None
                else AuditEventDraft(
                    event_type="ORDER_FILLED",
                    actor=audit_actor,
                    subject={"kind": "ORDER", "id": order_id},
                    payload={
                        "client_order_id": client_id,
                        "status": order.status.value,
                        "fill_id": fill_id,
                        "position_id": position_id,
                        "environment": intent.environment,
                    },
                    created_at=now,
                )
            ),
        )
        return BrokerResult(order=order, fill=fill, position=position, account=updated_account)


def _filled_status() -> Any:
    from oft.domain.oms import OrderStatus

    return OrderStatus.FILLED


def _complete_fill(order: Order, fill: Fill, *, at: str) -> Order:
    from dataclasses import replace

    return replace(
        order,
        filled_quantity=fill.quantity,
        avg_fill_price=fill.price,
        filled_at=at,
        submitted_at=at,
        updated_at=at,
        trace=(*order.trace, "FILLED"),
    )


def _order_record(order: Order) -> OrderRecord:
    return OrderRecord(
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        environment=order.environment,
        symbol=order.symbol,
        status=order.status.value,
        risk_verdict_id=order.risk_verdict_id,
        idempotency_key=order.idempotency_key,
        created_at=order.created_at,
        updated_at=order.updated_at,
        payload_json=json.dumps(order.to_contract(), sort_keys=True),
    )


def _fill_record(fill: Fill) -> FillRecord:
    return FillRecord(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        environment=fill.environment,
        symbol=fill.symbol,
        filled_at=fill.filled_at,
        payload_json=json.dumps(fill.to_contract(), sort_keys=True),
    )


def _position_record(position: Position) -> PositionRecord:
    return PositionRecord(
        position_id=position.position_id,
        account_id=position.account_id,
        environment=position.environment,
        symbol=position.symbol,
        state=position.state,
        opened_at=position.opened_at,
        payload_json=json.dumps(position.to_contract(), sort_keys=True),
    )
