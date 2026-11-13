"""Deterministic paper broker (ADR-0006).

Market fills at the mark/last price plus a configurable slippage (default
2 bps) and taker fee (default 5 bps). No queue modeling, no partial fills, no
randomness: the same fixed clock and inputs produce the same ids and values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
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
from oft.domain.pnl import plain_money_str
from oft.domain.positions import (
    CloseResult,
    close_full,
    position_from_contract,
    trade_id_for,
    trade_log_from_close,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    AuditEventDraft,
    ClosedPositionRecord,
    FillRecord,
    OrderRecord,
    PaperAccountRecord,
    PositionEventRecord,
    PositionRecord,
    StorePort,
    TradeLogRecord,
)

_EIGHT_DP = Decimal("0.00000001")
_DEFAULT_ACCOUNT_ID = "ACC-PAPER"


class BrokerError(Exception):
    code = "ORDER_REJECTED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


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
    trigger_id: str | None = None
    position_id: str | None = None
    reduce_only: bool = False

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
                "purpose": self.purpose,
                "position_id": self.position_id,
                "reduce_only": self.reduce_only,
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


def _cash_flow(side: OrderSide, quote_quantity: Decimal, fee: Decimal) -> Decimal:
    """The cash change for a filled market order.

    A SELL credits proceeds minus its fee; a BUY debits cost plus its fee.
    Entry and exit are exact inverses, so a full round trip changes cash by
    exactly the trade's net PnL (gross minus fees) for LONG and SHORT alike.
    """
    if side is OrderSide.SELL:
        return quote_quantity - fee
    return -(quote_quantity + fee)


class PaperBroker:
    def __init__(self, *, settings: Settings, store: StorePort, clock: ClockPort) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._slippage = Decimal(settings.paper_slippage_bps) / Decimal("10000")
        self._taker_fee = Decimal(settings.paper_taker_fee_bps) / Decimal("10000")

    def account(self) -> PaperAccountRecord:
        """Delegate to the account service so the seed and read model agree."""
        from oft.application.paper_account import PaperAccountService

        return PaperAccountService(
            settings=self._settings, store=self._store, clock=self._clock
        ).ensure()

    def submit(
        self,
        intent: OrderIntent,
        *,
        mark_price: Decimal,
        mark_as_of: str,
        now: str,
        audit_actor: dict[str, Any] | None = None,
    ) -> BrokerResult:
        # Defense in depth (S5): the gateway enforces PAPER, but the broker must
        # not fill a live/test intent even if a future caller reaches it.
        if intent.environment != "PAPER":
            raise BrokerError("the paper broker only accepts PAPER orders")
        if intent.quantity <= 0:
            raise BrokerError("quantity must be positive")
        if mark_price <= 0:
            raise BrokerError("mark price must be positive")
        if intent.reduce_only or intent.purpose.upper() == "EXIT":
            return self._submit_exit(
                intent,
                mark_price=mark_price,
                mark_as_of=mark_as_of,
                now=now,
                audit_actor=audit_actor,
            )
        direction = Decimal("1") if intent.side is OrderSide.BUY else Decimal("-1")
        fill_price = _quantize(mark_price * (Decimal("1") + direction * self._slippage))
        quote_quantity = _quantize(fill_price * intent.quantity)
        fee = _quantize(quote_quantity * self._taker_fee)
        account = self.account()
        # The store applies this delta to the balance it reads inside the write
        # transaction, so concurrent fills cannot overwrite each other. A SELL
        # (SHORT entry) credits proceeds; a BUY (LONG entry) debits the cost.
        balance_delta = str(_cash_flow(intent.side, quote_quantity, fee))

        digest = intent.hash()
        # D6: the deterministic client id binds the decision and the intent.
        client_id = client_order_id(
            intent.environment,
            intent.symbol,
            intent.purpose,
            f"{intent.decision_id}|{digest}",
        )
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
        if intent.trigger_id is not None:
            # The management path needs the originating trigger row so its exit
            # risk verdict can reference a real parent (never a fabricated one).
            position = replace(position, source_trigger_id=intent.trigger_id)
        updated_account = PaperAccountRecord(
            account_id=account.account_id,
            environment=account.environment,
            currency=account.currency,
            initial_balance=account.initial_balance,
            balance=account.balance,
            updated_at=now,
        )
        commit = self._store.save_order_fill_position(
            order=_order_record(order, intent),
            fill=_fill_record(fill),
            position=_position_record(position),
            account=updated_account,
            balance_delta=balance_delta,
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
        return BrokerResult(order=order, fill=fill, position=position, account=commit.account)

    def _submit_exit(
        self,
        intent: OrderIntent,
        *,
        mark_price: Decimal,
        mark_as_of: str,
        now: str,
        audit_actor: dict[str, Any] | None,
    ) -> BrokerResult:
        """Apply a reduce-only full close to the one existing position row.

        The position is updated in place by ``save_close``; no second position
        is ever created. Cash changes by the sale proceeds minus the fee for a
        LONG close, or by minus (buy-back + fee) for a SHORT close.
        """
        if not intent.reduce_only:
            raise BrokerError("an exit intent must be reduce-only")
        position = self._exit_position(intent)
        fill, order, close, fill_price, cash_delta = self._build_exit(
            intent, position, mark_price=mark_price, now=now
        )
        trade_id = trade_id_for(position.position_id)
        trade_payload = trade_log_from_close(close, trade_id=trade_id)
        commit = self._store.save_close(
            _close_record(
                intent=intent,
                order=order,
                fill=fill,
                position=position,
                close=close,
                account=self.account(),
                cash_delta=cash_delta,
                trade_id=trade_id,
                trade_payload=trade_payload,
                order_id=order.order_id,
                fill_id=fill.fill_id,
                fill_price=fill_price,
                now=now,
                audit_actor=audit_actor,
            )
        )
        return BrokerResult(
            order=order,
            fill=fill,
            position=close.position,
            account=commit.account,
        )

    def _build_exit(
        self,
        intent: OrderIntent,
        position: Position,
        *,
        mark_price: Decimal,
        now: str,
    ) -> tuple[Fill, Order, CloseResult, Decimal, Decimal]:
        """Fill, order, close, and cash delta for one validated exit intent."""
        fill_price, quote_quantity, fee, cash_delta = self._exit_amounts(intent, mark_price)
        digest, client_id, order_id, fill_id = _exit_ids(intent)
        fill = _fill_for(
            intent,
            fill_id=fill_id,
            order_id=order_id,
            price=fill_price,
            quote_quantity=quote_quantity,
            fee=fee,
            now=now,
        )
        order = _exit_order(
            intent,
            position=position,
            client_id=client_id,
            order_id=order_id,
            digest=digest,
            fill=fill,
            now=now,
        )
        return fill, order, close_full(position, fill, at=now), fill_price, cash_delta

    def _exit_position(self, intent: OrderIntent) -> Position:
        """Load and validate the one OPEN position an exit intent targets."""
        if intent.position_id is None:
            raise BrokerError("an exit intent requires a position id")
        record = self._store.get_position(intent.position_id)
        if record is None:
            raise BrokerError("the position to close does not exist", code="POSITION_NOT_FOUND")
        position = position_from_contract(json.loads(record.payload_json))
        if position.state != "OPEN":
            raise BrokerError("the position is not OPEN", code="POSITION_STATE_INVALID")
        if position.symbol != intent.symbol or position.side is not intent.position_side:
            raise BrokerError("the exit intent does not match the position")
        if intent.quantity != position.size:
            raise BrokerError("only a full close is supported")
        if position.source_trigger_id is None:
            raise BrokerError(
                "the position has no source trigger; refusing to fabricate one",
                code="DATA_UNAVAILABLE",
            )
        expected_side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
        if intent.side is not expected_side:
            raise BrokerError("the exit side is wrong for the position")
        return position

    def _exit_amounts(
        self, intent: OrderIntent, mark_price: Decimal
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Return the exit ``(fill price, quote, fee, cash delta)``."""
        direction = Decimal("-1") if intent.side is OrderSide.SELL else Decimal("1")
        fill_price = _quantize(mark_price * (Decimal("1") + direction * self._slippage))
        quote_quantity = _quantize(fill_price * intent.quantity)
        fee = _quantize(quote_quantity * self._taker_fee)
        return fill_price, quote_quantity, fee, _cash_flow(intent.side, quote_quantity, fee)


def _exit_ids(intent: OrderIntent) -> tuple[str, str, str, str]:
    """The deterministic ``(digest, client id, order id, fill id)`` of an exit."""
    digest = intent.hash()
    client_id = client_order_id(
        intent.environment,
        intent.symbol,
        intent.purpose,
        f"{intent.decision_id}|{digest}",
    )
    order_id = deterministic_id("ORD", f"{client_id}:order")
    fill_id = deterministic_id("FIL", f"{client_id}:fill")
    return digest, client_id, order_id, fill_id


def _close_record(
    *,
    intent: OrderIntent,
    order: Order,
    fill: Fill,
    position: Position,
    close: CloseResult,
    account: PaperAccountRecord,
    cash_delta: Decimal,
    trade_id: str,
    trade_payload: dict[str, Any],
    order_id: str,
    fill_id: str,
    fill_price: Decimal,
    now: str,
    audit_actor: dict[str, Any] | None,
) -> ClosedPositionRecord:
    """Assemble every row one close commits in a single transaction."""
    return ClosedPositionRecord(
        order=_order_record(order, intent),
        fill=_fill_record(fill),
        position=_closed_position_record(position, close),
        account=_account_shell(account, now),
        balance_delta=str(cash_delta),
        event=_exit_event(position.position_id, at=now, order_id=order_id),
        trade_log=_trade_log_record(
            intent=intent,
            position=position,
            close=close,
            trade_id=trade_id,
            trade_payload=trade_payload,
            order_id=order_id,
            fill_id=fill_id,
            now=now,
        ),
        audits=_exit_audits(
            intent=intent,
            position=position,
            close=close,
            order_id=order_id,
            fill_id=fill_id,
            trade_id=trade_id,
            fill_price=fill_price,
            now=now,
            audit_actor=audit_actor,
        ),
    )


def _fill_for(
    intent: OrderIntent,
    *,
    fill_id: str,
    order_id: str,
    price: Decimal,
    quote_quantity: Decimal,
    fee: Decimal,
    now: str,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        environment=intent.environment,
        account_id=intent.account_id,
        symbol=intent.symbol,
        side=intent.side,
        price=price,
        quantity=intent.quantity,
        quote_quantity=quote_quantity,
        fee=fee,
        fee_asset="USDT",
        maker=False,
        filled_at=now,
        received_at=now,
        source="PAPER",
    )


def _exit_order(
    intent: OrderIntent,
    *,
    position: Position,
    client_id: str,
    order_id: str,
    digest: str,
    fill: Fill,
    now: str,
) -> Order:
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
        position_id=position.position_id,
        reduce_only=True,
    )
    return _complete_fill(order, fill, at=now)


def _closed_position_record(position: Position, close: CloseResult) -> PositionRecord:
    return PositionRecord(
        position_id=position.position_id,
        account_id=position.account_id,
        environment=position.environment,
        symbol=position.symbol,
        state=close.position.state,
        opened_at=position.opened_at,
        payload_json=json.dumps(close.position.to_contract(), sort_keys=True),
    )


def _account_shell(account: PaperAccountRecord, now: str) -> PaperAccountRecord:
    return PaperAccountRecord(
        account_id=account.account_id,
        environment=account.environment,
        currency=account.currency,
        initial_balance=account.initial_balance,
        balance=account.balance,
        updated_at=now,
    )


def _exit_event(position_id: str, *, at: str, order_id: str) -> PositionEventRecord:
    return PositionEventRecord(
        position_id=position_id,
        seq=0,
        at=at,
        action="FULL_CLOSE",
        order_id=order_id,
        note="protective stop touched",
    )


def _trade_log_record(
    *,
    intent: OrderIntent,
    position: Position,
    close: CloseResult,
    trade_id: str,
    trade_payload: dict[str, Any],
    order_id: str,
    fill_id: str,
    now: str,
) -> TradeLogRecord:
    return TradeLogRecord(
        trade_id=trade_id,
        position_id=position.position_id,
        account_id=position.account_id,
        environment=position.environment,
        symbol=position.symbol,
        side=position.side.value,
        opened_at=position.opened_at,
        closed_at=now,
        net_pnl_usd=plain_money_str(close.net_pnl_usd),
        idempotency_key=intent.idempotency_key,
        payload_json=json.dumps(trade_payload, sort_keys=True),
        order_id=order_id,
        fill_id=fill_id,
        risk_verdict_id=intent.risk_verdict_id,
        decision_id=intent.decision_id,
    )


def _exit_audits(
    *,
    intent: OrderIntent,
    position: Position,
    close: CloseResult,
    order_id: str,
    fill_id: str,
    trade_id: str,
    fill_price: Decimal,
    now: str,
    audit_actor: dict[str, Any] | None,
) -> tuple[AuditEventDraft, ...]:
    if audit_actor is None:
        return ()
    return (
        AuditEventDraft(
            event_type="POSITION_CLOSED",
            actor=audit_actor,
            subject={"kind": "POSITION", "id": position.position_id},
            payload={
                "order_id": order_id,
                "fill_id": fill_id,
                "exit_price": str(fill_price),
                "realized_pnl_usd": str(close.net_pnl_usd),
                "rule": "STOP_TOUCH",
                "environment": intent.environment,
            },
            created_at=now,
        ),
        AuditEventDraft(
            event_type="TRADE_LOGGED",
            actor=audit_actor,
            subject={"kind": "TRADE_LOG", "id": trade_id},
            payload={
                "position_id": position.position_id,
                "trade_id": trade_id,
                "net_pnl_usd": str(close.net_pnl_usd),
            },
            created_at=now,
        ),
    )


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


def _order_record(order: Order, intent: OrderIntent) -> OrderRecord:
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
        trigger_id=intent.trigger_id,
        decision_id=intent.decision_id,
        purpose=intent.purpose.upper(),
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
