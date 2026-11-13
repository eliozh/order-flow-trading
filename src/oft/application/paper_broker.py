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
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    client_order_id,
    deterministic_id,
    intent_hash,
    order_from_intent,
    position_from_fill,
)
from oft.domain.pnl import (
    MAX_ABS_MONEY,
    MAX_ABS_PRICE,
    MAX_ABS_QUANTITY,
    money_str,
    quantize_money,
)
from oft.domain.positions import (
    STOP_TOUCH_RULE,
    AddResult,
    CloseResult,
    ManagementEvent,
    PositionError,
    TradeLogContext,
    accumulated_realized_gross,
    apply_add,
    close_full,
    partial_close,
    position_from_contract,
    trade_id_for,
    trade_log_from_close,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    AuditEventDraft,
    ClosedPositionRecord,
    FillRecord,
    OmsConflictError,
    OrderRecord,
    PaperAccountRecord,
    PositionAdjustmentRecord,
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
    # Service-owned journal facts rendered atomically with a full close. It is
    # deliberately excluded from ``hash()``: it is derived from the position, not
    # part of the order intent.
    trade_log: TradeLogContext | None = None

    def __post_init__(self) -> None:
        # One closed representation: ``purpose`` is the authority and
        # ``reduce_only`` is derived from it, so the gateway (AND) and the
        # broker (OR) can never disagree. The intent hash now includes the
        # normalized purpose and the derived flag.
        purpose = self.purpose.upper()
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "reduce_only", purpose == "EXIT")

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


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _fill_amounts(
    side: OrderSide,
    mark_price: Decimal,
    quantity: Decimal,
    *,
    slippage: Decimal,
    taker_fee: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(fill price, quote quantity, fee)`` for a filled market order.

    A BUY fills above the mark and a SELL below it; both entry and exit use
    this one expression so the money math cannot drift.
    """
    direction = Decimal("1") if side is OrderSide.BUY else Decimal("-1")
    fill_price = _quantize(mark_price * (Decimal("1") + direction * slippage))
    quote_quantity = _quantize(fill_price * quantity)
    fee = _quantize(quote_quantity * taker_fee)
    return fill_price, quote_quantity, fee


def _intent_ids(intent: OrderIntent) -> tuple[str, str, str, str]:
    """The deterministic ``(digest, client id, order id, fill id)`` of an intent."""
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


def _cash_flow(side: OrderSide, quote_quantity: Decimal, fee: Decimal) -> Decimal:
    """The cash change for a filled market order.

    A SELL credits proceeds minus its fee; a BUY debits cost plus its fee.
    Entry and exit are exact inverses, so a full round trip changes cash by
    exactly the trade's net PnL (gross minus fees) for LONG and SHORT alike.
    """
    if side is OrderSide.SELL:
        return quote_quantity - fee
    return -(quote_quantity + fee)


def recorded_entry_notional_usd(store: StorePort, trigger_id: str | None) -> Decimal | None:
    """The immutable entry notional: the recorded entry fill's quote quantity.

    A partial close scales the mutable read-model ``notional_usd`` down to the
    remaining exposure, so the journal's ``return_pct`` and entry-notional
    context cannot use it. This resolves the entry basis from the same recorded
    entry order/fill the position was opened from. Missing or implausible
    recorded data yields ``None`` and the caller falls back to a named gap.
    """
    if trigger_id is None:
        return None
    order = store.get_entry_order_for_trigger(trigger_id)
    if order is None:
        return None
    fill = store.get_fill_by_order_id(order.order_id)
    if fill is None:
        return None
    try:
        payload = json.loads(fill.payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    quote = payload.get("quote_quantity")
    if quote is not None:
        try:
            value = Decimal(str(quote))
        except ArithmeticError:
            value = None
        if value is not None and value.is_finite() and 0 < value <= MAX_ABS_MONEY:
            return quantize_money(value)
    price = payload.get("price")
    quantity = payload.get("quantity")
    try:
        price_value = Decimal(str(price))
        quantity_value = Decimal(str(quantity))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if (
        not price_value.is_finite()
        or not quantity_value.is_finite()
        or price_value <= 0
        or quantity_value <= 0
        or price_value > MAX_ABS_PRICE
        or quantity_value > MAX_ABS_QUANTITY
    ):
        return None
    try:
        return quantize_money(price_value * quantity_value)
    except ValueError:
        return None


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
        if intent.purpose == "ADD":
            return self._submit_add(
                intent,
                mark_price=mark_price,
                now=now,
                audit_actor=audit_actor,
            )
        if intent.reduce_only:
            return self._submit_exit(
                intent,
                mark_price=mark_price,
                mark_as_of=mark_as_of,
                now=now,
                audit_actor=audit_actor,
            )
        fill_price, quote_quantity, fee = _fill_amounts(
            intent.side,
            mark_price,
            intent.quantity,
            slippage=self._slippage,
            taker_fee=self._taker_fee,
        )
        account = self.account()
        # The store applies this delta to the balance it reads inside the write
        # transaction, so concurrent fills cannot overwrite each other. A SELL
        # (SHORT entry) credits proceeds; a BUY (LONG entry) debits the cost.
        balance_delta = str(_cash_flow(intent.side, quote_quantity, fee))

        digest, client_id, order_id, fill_id = _intent_ids(intent)
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
        """Apply a reduce-only fill to the one existing position row.

        A quantity equal to the position's size is a full close: the row flips
        to CLOSED and exactly one trade log is written atomically. A smaller
        quantity is a partial close: the row stays OPEN, accumulates realized
        PnL/fees, and no trade log is written. No second position is ever
        created.
        """
        if not intent.reduce_only:
            raise BrokerError("an exit intent must be reduce-only")
        position, raw_payload, raw_json = self._load_position(intent)
        if intent.side is not _reducing_side(position.side):
            raise BrokerError("the exit side is wrong for the position")
        # A replay is recognized before the size checks: after a committed
        # partial close the residual size no longer matches the intent's slice.
        existing = self._store.get_order_by_idempotency(intent.idempotency_key)
        if existing is not None:
            return self._replay_adjustment(intent, existing, position)
        if intent.quantity > position.size:
            raise BrokerError("the exit quantity exceeds the position size")
        if intent.quantity < position.size:
            _validate_step_multiple(self._store, position.symbol, intent.quantity)
            return self._submit_partial_close(
                intent,
                position,
                raw_payload,
                raw_json,
                mark_price=mark_price,
                now=now,
                audit_actor=audit_actor,
            )
        fill, order, close, fill_price, cash_delta = self._build_exit(
            intent,
            position,
            prior_gross=accumulated_realized_gross(raw_payload),
            mark_price=mark_price,
            now=now,
        )
        trade_id = trade_id_for(position.position_id)
        expected_entry_notional = (
            recorded_entry_notional_usd(self._store, position.source_trigger_id)
            if intent.trade_log is not None
            else None
        )
        try:
            trade_payload = trade_log_from_close(
                close,
                trade_id=trade_id,
                context=intent.trade_log,
                expected_entry_notional_usd=expected_entry_notional,
            )
        except PositionError as exc:
            raise BrokerError(exc.message, code=exc.code) from exc
        try:
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
                    expected_position_json=raw_json,
                )
            )
        except OmsConflictError as exc:
            if exc.constraint == "position_version":
                raise BrokerError(
                    "the position changed concurrently; retry the close", code="CONFLICT"
                ) from exc
            raise
        return BrokerResult(
            order=order,
            fill=fill,
            position=close.position,
            account=commit.account,
        )

    def _submit_partial_close(
        self,
        intent: OrderIntent,
        position: Position,
        raw_payload: dict[str, Any],
        raw_json: str,
        *,
        mark_price: Decimal,
        now: str,
        audit_actor: dict[str, Any] | None,
    ) -> BrokerResult:
        """Apply a reduce-only slice; the position stays OPEN, no trade log."""
        fill_price, quote_quantity, fee, cash_delta = self._exit_amounts(intent, mark_price)
        digest, client_id, order_id, fill_id = _intent_ids(intent)
        fill = _fill_for(
            intent,
            fill_id=fill_id,
            order_id=order_id,
            price=fill_price,
            quote_quantity=quote_quantity,
            fee=fee,
            now=now,
        )
        order = _adjustment_order(
            intent,
            client_id=client_id,
            order_id=order_id,
            digest=digest,
            fill=fill,
            position_id=position.position_id,
            reduce_only=True,
            now=now,
        )
        try:
            result = partial_close(
                position,
                fill,
                at=now,
                decision_id=intent.decision_id,
                accumulated_gross=accumulated_realized_gross(raw_payload),
            )
        except PositionError as exc:
            raise BrokerError(exc.message, code=exc.code) from exc
        commit = self._store.save_position_adjustment(
            _adjustment_record(
                intent=intent,
                order=order,
                fill=fill,
                position_payload=result.payload,
                event=result.event,
                position_id=position.position_id,
                account=self.account(),
                cash_delta=cash_delta,
                symbol=position.symbol,
                now=now,
                audit_actor=audit_actor,
                audit_event_type="POSITION_PARTIAL_CLOSE",
                note="partial close",
                expected_payload_json=raw_json,
            )
        )
        updated = position_from_contract(result.payload)
        return BrokerResult(order=order, fill=fill, position=updated, account=commit.account)

    def _submit_add(
        self,
        intent: OrderIntent,
        *,
        mark_price: Decimal,
        now: str,
        audit_actor: dict[str, Any] | None,
    ) -> BrokerResult:
        """Apply a position-increasing fill in place; no duplicate position."""
        position, raw_payload, raw_json = self._load_position(intent)
        existing = self._store.get_order_by_idempotency(intent.idempotency_key)
        if existing is not None:
            return self._replay_adjustment(intent, existing, position)
        _validate_step_multiple(self._store, position.symbol, intent.quantity)
        _require_add_verdict(self._store, position, intent, now=now)
        if intent.side is not _increasing_side(position.side):
            raise BrokerError("the add side is wrong for the position")
        fill_price, quote_quantity, fee = _fill_amounts(
            intent.side,
            mark_price,
            intent.quantity,
            slippage=self._slippage,
            taker_fee=self._taker_fee,
        )
        cash_delta = _cash_flow(intent.side, quote_quantity, fee)
        digest, client_id, order_id, fill_id = _intent_ids(intent)
        fill = _fill_for(
            intent,
            fill_id=fill_id,
            order_id=order_id,
            price=fill_price,
            quote_quantity=quote_quantity,
            fee=fee,
            now=now,
        )
        order = _adjustment_order(
            intent,
            client_id=client_id,
            order_id=order_id,
            digest=digest,
            fill=fill,
            position_id=position.position_id,
            reduce_only=False,
            now=now,
        )
        try:
            result: AddResult = apply_add(
                position,
                fill,
                at=now,
                decision_id=intent.decision_id,
                accumulated_gross=accumulated_realized_gross(raw_payload),
            )
        except PositionError as exc:
            raise BrokerError(exc.message, code=exc.code) from exc
        commit = self._store.save_position_adjustment(
            _adjustment_record(
                intent=intent,
                order=order,
                fill=fill,
                position_payload=result.payload,
                event=result.event,
                position_id=position.position_id,
                account=self.account(),
                cash_delta=cash_delta,
                symbol=position.symbol,
                now=now,
                audit_actor=audit_actor,
                audit_event_type="POSITION_ADDED",
                note="add to position",
                expected_payload_json=raw_json,
            )
        )
        return BrokerResult(
            order=order,
            fill=fill,
            position=result.position,
            account=commit.account,
        )

    def _build_exit(
        self,
        intent: OrderIntent,
        position: Position,
        *,
        prior_gross: Decimal,
        mark_price: Decimal,
        now: str,
    ) -> tuple[Fill, Order, CloseResult, Decimal, Decimal]:
        """Fill, order, close, and cash delta for one validated full exit."""
        fill_price, quote_quantity, fee, cash_delta = self._exit_amounts(intent, mark_price)
        digest, client_id, order_id, fill_id = _intent_ids(intent)
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
        close_reason = STOP_TOUCH_RULE
        try:
            close = close_full(
                position,
                fill,
                at=now,
                decision_id=intent.decision_id,
                accumulated_gross=prior_gross,
                close_reason=close_reason,
            )
        except PositionError as exc:
            raise BrokerError(exc.message, code=exc.code) from exc
        return fill, order, close, fill_price, cash_delta

    def _load_position(self, intent: OrderIntent) -> tuple[Position, dict[str, Any], str]:
        """Load and validate the one OPEN position an exit/add intent targets."""
        if intent.position_id is None:
            raise BrokerError("a management intent requires a position id")
        record = self._store.get_position(intent.position_id)
        if record is None:
            raise BrokerError("the position does not exist", code="POSITION_NOT_FOUND")
        raw_json = record.payload_json
        raw_payload = json.loads(raw_json)
        position = position_from_contract(raw_payload)
        if position.state != "OPEN":
            raise BrokerError("the position is not OPEN", code="POSITION_STATE_INVALID")
        if position.symbol != intent.symbol or position.side is not intent.position_side:
            raise BrokerError("the management intent does not match the position")
        if position.source_trigger_id is None:
            raise BrokerError(
                "the position has no source trigger; refusing to fabricate one",
                code="DATA_UNAVAILABLE",
            )
        return position, raw_payload, raw_json

    def _replay_adjustment(
        self,
        intent: OrderIntent,
        existing: OrderRecord,
        position: Position,
    ) -> BrokerResult:
        """Return the committed adjustment for a replayed idempotency key."""
        try:
            order_payload = json.loads(existing.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BrokerError(
                "the existing adjustment order payload is unreadable",
                code="DATA_UNAVAILABLE",
            ) from exc
        if (
            not isinstance(order_payload, dict)
            or str(order_payload.get("intent_hash") or "") != intent.hash()
        ):
            raise BrokerError(
                f"idempotency key {intent.idempotency_key} was used for a different adjustment",
                code="IDEMPOTENCY_CONFLICT",
            )
        fill_record = self._store.get_fill_by_order_id(existing.order_id)
        if fill_record is None:
            raise BrokerError(
                f"the existing adjustment fill for {existing.order_id} is missing",
                code="DATA_UNAVAILABLE",
            )
        try:
            order = _order_from_payload(order_payload)
            fill = _fill_from_payload(json.loads(fill_record.payload_json))
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerError(
                "the existing adjustment payload is corrupt",
                code="DATA_UNAVAILABLE",
            ) from exc
        return BrokerResult(
            order=order,
            fill=fill,
            position=position,
            account=self.account(),
        )

    def _exit_amounts(
        self, intent: OrderIntent, mark_price: Decimal
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Return the exit ``(fill price, quote, fee, cash delta)``."""
        fill_price, quote_quantity, fee = _fill_amounts(
            intent.side,
            mark_price,
            intent.quantity,
            slippage=self._slippage,
            taker_fee=self._taker_fee,
        )
        return fill_price, quote_quantity, fee, _cash_flow(intent.side, quote_quantity, fee)


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
    expected_position_json: str | None = None,
) -> ClosedPositionRecord:
    """Assemble every row one close commits in a single transaction."""
    return ClosedPositionRecord(
        order=_order_record(order, intent),
        fill=_fill_record(fill),
        position=_closed_position_record(position, close, expected_position_json),
        account=_account_shell(account, now),
        balance_delta=str(cash_delta),
        event=_exit_event(position.position_id, close.event),
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


def _closed_position_record(
    position: Position, close: CloseResult, expected_payload_json: str | None = None
) -> PositionRecord:
    return PositionRecord(
        position_id=position.position_id,
        account_id=position.account_id,
        environment=position.environment,
        symbol=position.symbol,
        state=close.position.state,
        opened_at=position.opened_at,
        payload_json=json.dumps(close.position.to_contract(), sort_keys=True),
        expected_payload_json=expected_payload_json,
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


def _exit_event(position_id: str, event: ManagementEvent) -> PositionEventRecord:
    """The append-only audit projection of the close's single management event."""
    return PositionEventRecord(
        position_id=position_id,
        seq=None,
        at=event.at,
        action=event.action,
        order_id=event.order_id,
        note=event.note,
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
        net_pnl_usd=money_str(close.net_pnl_usd),
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


def _reducing_side(side: PositionSide) -> OrderSide:
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def _increasing_side(side: PositionSide) -> OrderSide:
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def _validate_step_multiple(store: StorePort, symbol: str, quantity: Decimal) -> None:
    """Reject an adjustment size that is not a multiple of the recorded step."""
    contract = store.get_symbol(symbol)
    if contract is None or contract.step_size <= 0:
        # No recorded contract basis: there is nothing to normalize against.
        return
    if quantity % contract.step_size != 0:
        raise BrokerError(
            f"quantity {quantity} is not a multiple of the recorded step size {contract.step_size}",
            code="ORDER_REJECTED",
        )


def _require_add_verdict(
    store: StorePort, position: Position, intent: OrderIntent, *, now: str
) -> None:
    """An ADD must carry a usable, persisted verdict bound to this position.

    The public ADD route is slice 25, so this is the broker-level gate: the
    verdict must exist, be bound to the source trigger and the add decision, be
    an ``ALLOW`` that has not expired, and cover the add's quantity/leverage.
    Kill-switch and gateway routing of ADD remain slice 25.
    """
    verdict = store.get_risk_verdict(intent.risk_verdict_id)
    if verdict is None:
        raise BrokerError("an add requires a persisted risk verdict", code="RISK_VERDICT_MISSING")
    if verdict.trigger_id != position.source_trigger_id:
        raise BrokerError(
            "the add risk verdict is not bound to the position's source trigger",
            code="RISK_VERDICT_INVALID",
        )
    if verdict.decision_id != intent.decision_id:
        raise BrokerError(
            "the add risk verdict is not bound to the add decision",
            code="RISK_VERDICT_INVALID",
        )
    if verdict.verdict == "BLOCK":
        raise BrokerError("the risk verdict blocks this add", code="RISK_BLOCKED")
    if verdict.expires_at <= now:
        raise BrokerError("the add risk verdict has expired", code="RISK_BLOCKED")
    try:
        payload = json.loads(verdict.payload_json)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BrokerError(
            "the add risk verdict payload is unreadable", code="RISK_VERDICT_INVALID"
        ) from exc
    allowed = payload.get("allowed") if isinstance(payload, dict) else None
    if not isinstance(allowed, dict):
        raise BrokerError(
            "the add risk verdict records no allowed cap", code="RISK_VERDICT_INVALID"
        )
    allowed_quantity = _decimal_or_none(allowed.get("quantity"))
    if allowed_quantity is None or allowed_quantity <= 0 or intent.quantity > allowed_quantity:
        raise BrokerError(
            "the add quantity exceeds the risk verdict's allowed quantity",
            code="RISK_VERDICT_INVALID",
        )
    allowed_leverage = _decimal_or_none(allowed.get("leverage"))
    if allowed_leverage is None or Decimal(str(intent.leverage)) > allowed_leverage:
        raise BrokerError(
            "the add leverage exceeds the risk verdict's allowed leverage",
            code="RISK_VERDICT_INVALID",
        )


def _order_from_payload(payload: dict[str, Any]) -> Order:
    """Rebuild a domain ``Order`` from its persisted contract payload."""
    return Order(
        order_id=str(payload["order_id"]),
        client_order_id=str(payload["client_order_id"]),
        environment=str(payload["environment"]),
        account_id=str(payload["account_id"]),
        position_id=payload.get("position_id"),
        symbol=str(payload["symbol"]),
        side=OrderSide(str(payload["side"])),
        type=OrderType(str(payload["type"])),
        time_in_force=str(payload.get("time_in_force") or "GTC"),
        quantity=Decimal(str(payload["quantity"])),
        price=None if payload.get("price") is None else Decimal(str(payload["price"])),
        stop_price=(
            None if payload.get("stop_price") is None else Decimal(str(payload["stop_price"]))
        ),
        reduce_only=bool(payload.get("reduce_only", False)),
        status=OrderStatus(str(payload["status"])),
        risk_verdict_id=str(payload.get("risk_verdict_id") or ""),
        approval_id=payload.get("approval_id"),
        intent_hash=str(payload.get("intent_hash") or ""),
        idempotency_key=str(payload.get("idempotency_key") or ""),
        created_at=str(payload["created_at"]),
        submitted_at=payload.get("submitted_at"),
        updated_at=str(payload["updated_at"]),
        filled_at=payload.get("filled_at"),
        avg_fill_price=(
            None
            if payload.get("avg_fill_price") is None
            else Decimal(str(payload["avg_fill_price"]))
        ),
        filled_quantity=Decimal(str(payload.get("filled_quantity") or "0")),
    )


def _fill_from_payload(payload: dict[str, Any]) -> Fill:
    """Rebuild a domain ``Fill`` from its persisted contract payload."""
    return Fill(
        fill_id=str(payload["fill_id"]),
        order_id=str(payload["order_id"]),
        environment=str(payload["environment"]),
        account_id=str(payload["account_id"]),
        symbol=str(payload["symbol"]),
        side=OrderSide(str(payload["side"])),
        price=Decimal(str(payload["price"])),
        quantity=Decimal(str(payload["quantity"])),
        quote_quantity=Decimal(str(payload["quote_quantity"])),
        fee=Decimal(str(payload["fee"])),
        fee_asset=str(payload.get("fee_asset") or "USDT"),
        maker=bool(payload.get("maker", False)),
        filled_at=str(payload["filled_at"]),
        received_at=str(payload["received_at"]),
        source=str(payload.get("source") or "PAPER"),
        exchange_trade_id=payload.get("exchange_trade_id"),
    )


def _adjustment_order(
    intent: OrderIntent,
    *,
    client_id: str,
    order_id: str,
    digest: str,
    fill: Fill,
    position_id: str,
    reduce_only: bool,
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
        position_id=position_id,
        reduce_only=reduce_only,
    )
    return _complete_fill(order, fill, at=now)


def _adjustment_record(
    *,
    intent: OrderIntent,
    order: Order,
    fill: Fill,
    position_payload: dict[str, Any],
    event: ManagementEvent,
    position_id: str,
    account: PaperAccountRecord,
    cash_delta: Decimal,
    symbol: str,
    now: str,
    audit_actor: dict[str, Any] | None,
    audit_event_type: str,
    note: str,
    expected_payload_json: str | None = None,
) -> PositionAdjustmentRecord:
    audits: tuple[AuditEventDraft, ...] = ()
    if audit_actor is not None:
        audits = (
            AuditEventDraft(
                event_type=audit_event_type,
                actor=audit_actor,
                subject={"kind": "POSITION", "id": position_id},
                payload={
                    "order_id": order.order_id,
                    "fill_id": fill.fill_id,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                    "action": event.action,
                    "note": note,
                    "environment": intent.environment,
                },
                created_at=now,
            ),
        )
    return PositionAdjustmentRecord(
        order=_order_record(order, intent),
        fill=_fill_record(fill),
        position=PositionRecord(
            position_id=position_id,
            account_id=account.account_id,
            environment=account.environment,
            symbol=symbol,
            state="OPEN",
            opened_at=str(position_payload.get("opened_at") or now),
            payload_json=json.dumps(position_payload, sort_keys=True),
            expected_payload_json=expected_payload_json,
        ),
        account=account,
        balance_delta=str(cash_delta),
        event=_exit_event(position_id, event),
        audits=audits,
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
