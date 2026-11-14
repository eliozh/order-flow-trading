"""The sole submit path: kill switch, risk verdict, human approval, idempotency.

No API/CLI route may call a broker directly. The gateway is the only writer of
orders and it refuses an intent without a persisted, fresh, non-blocking risk
verdict and a human approval (ADR-0006).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from oft.application.kill_switch import KillSwitch, record_audit
from oft.application.paper_account import compute_equity
from oft.application.paper_broker import (
    REDUCE_ONLY_PURPOSES,
    BrokerResult,
    OrderIntent,
    PaperBroker,
)
from oft.domain.risk import RiskRequest, verdict_inputs_hash
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import OmsConflictError, RiskVerdictRecord, StorePort

_RISK_REDUCING_PURPOSES = REDUCE_ONLY_PURPOSES


def _decimal_or_none(value: object) -> Decimal | None:
    """Parse an exact decimal; ``None`` and the contract's "0" mean absent."""
    if value is None or value == "0":
        return None
    return Decimal(str(value))


def _decimal_or_zero(value: object) -> Decimal | None:
    """Parse a measured decimal; only ``None`` means "not measured".

    ``daily_loss_usd`` is persisted as SQL ``NULL`` when unmeasured and as the
    string ``"0"`` for a measured zero, so the digest stays stable.
    """
    if value is None:
        return None
    return Decimal(str(value))


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthContext:
    actor_id: str
    kind: str = "USER"
    approval_id: str | None = None


@dataclass(frozen=True)
class IntentClassification:
    """Whether an intent increases or reduces risk (ADR-0006 kill-switch policy)."""

    risk_reducing: bool
    reason: str


def classify_intent(intent: OrderIntent) -> IntentClassification:
    """A reduce-only exit/partial/full close reduces risk; everything else increases it."""
    if intent.reduce_only and intent.purpose.upper() in _RISK_REDUCING_PURPOSES:
        return IntentClassification(True, "reduce-only exit")
    return IntentClassification(False, "risk-increasing intent")


class ExecutionGateway:
    def __init__(
        self,
        *,
        store: StorePort,
        broker: PaperBroker,
        kill_switch: KillSwitch,
        clock: ClockPort,
        environment: str = "PAPER",
    ) -> None:
        self._store = store
        self._broker = broker
        self._kill_switch = kill_switch
        self._clock = clock
        self._environment = environment

    def submit(
        self,
        intent: OrderIntent,
        auth: AuthContext,
        *,
        mark_price: Decimal,
        mark_as_of: str,
    ) -> BrokerResult:
        now = utc_timestamp(self._clock.now())
        classification = classify_intent(intent)
        # Audit the classification before the kill-switch decision so a blocked
        # risk-increasing intent is still on the record (ADR-0006).
        record_audit(
            self._store,
            event_type="INTENT_CLASSIFIED",
            actor={"kind": "SYSTEM", "id": "execution_gateway"},
            subject={"kind": "ORDER_INTENT", "id": intent.idempotency_key},
            payload={
                "risk_reducing": classification.risk_reducing,
                "reason": classification.reason,
                "purpose": intent.purpose,
                "reduce_only": intent.reduce_only,
                "position_id": intent.position_id,
            },
            at=now,
        )
        # The kill switch blocks risk-increasing intents but must permit the
        # reduce-only protective close while active.
        if not classification.risk_reducing:
            self._kill_switch.require_clear()
        if intent.environment != self._environment or intent.environment != "PAPER":
            raise GatewayError("ORDER_REJECTED", "only PAPER orders are supported")
        if auth.approval_id is None:
            raise GatewayError("ORDER_REJECTED", "human paper approval is required")
        verdict = self._store.get_risk_verdict(intent.risk_verdict_id)
        if verdict is None:
            raise GatewayError("RISK_BLOCKED", "a persisted risk verdict is required")
        if verdict.verdict == "BLOCK":
            raise GatewayError("RISK_BLOCKED", "the risk verdict blocks this intent")
        if verdict.expires_at <= now:
            raise GatewayError("RISK_BLOCKED", "the risk verdict has expired")

        existing = self._store.get_order_by_idempotency(intent.idempotency_key)
        if existing is not None:
            payload = json.loads(existing.payload_json)
            if str(payload.get("intent_hash") or "") != intent.hash():
                raise GatewayError(
                    "IDEMPOTENCY_CONFLICT",
                    f"idempotency key {intent.idempotency_key} was used for a different intent",
                )
            raise GatewayError("ORDER_DUPLICATE", f"order already exists: {payload['order_id']}")

        if classification.risk_reducing:
            self._bind_exit_to_verdict(intent, verdict, mark_price=mark_price)
        else:
            self._bind_to_verdict(intent, verdict, mark_price=mark_price)

        try:
            result = self._broker.submit(
                intent,
                mark_price=mark_price,
                mark_as_of=mark_as_of,
                now=now,
                audit_actor={"kind": auth.kind, "id": auth.actor_id},
            )
        except OmsConflictError as exc:
            # A concurrent submit can pass the read-only duplicate pre-check
            # and lose the unique index inside the commit. Never leak a 500:
            # replay the winner's order or report a typed conflict (DB-2/D6).
            raise self._conflict_error(intent, exc) from exc
        return result

    def _conflict_error(self, intent: OrderIntent, exc: OmsConflictError) -> GatewayError:
        if exc.constraint == "idempotency_key":
            existing = self._store.get_order_by_idempotency(intent.idempotency_key)
            if existing is not None:
                payload = json.loads(existing.payload_json)
                if str(payload.get("intent_hash") or "") == intent.hash():
                    return GatewayError(
                        "ORDER_DUPLICATE", f"order already exists: {existing.order_id}"
                    )
                return GatewayError(
                    "IDEMPOTENCY_CONFLICT",
                    f"idempotency key {intent.idempotency_key} was used for a different intent",
                )
        if exc.constraint == "trigger_entry":
            return GatewayError("ORDER_DUPLICATE", "an entry order already exists for this trigger")
        return GatewayError("ORDER_DUPLICATE", "a concurrent submit already committed this intent")

    def _bind_exit_to_verdict(
        self,
        intent: OrderIntent,
        record: RiskVerdictRecord,
        *,
        mark_price: Decimal,
    ) -> None:
        """Bind a reduce-only exit to its persisted verdict and target position.

        An exit does not need the entry trigger/stop/equity-digest binding: the
        verdict only has to belong to the same decision, cover the exit
        quantity, name the same symbol/side as the position, and the verdict's
        trigger must equal the position's own source trigger. The intent must be
        reduce-only against an OPEN position that carries a source trigger.
        ADR-0006 also forbids trusting the caller: the submitted close mark must
        equal the position's recorded mark, so an exit cannot be priced from an
        invented or stale price that the position row does not carry.
        """
        if not intent.reduce_only:
            raise GatewayError("RISK_BLOCKED", "an exit intent must be reduce-only")
        payload = json.loads(record.payload_json)
        if record.decision_id != intent.decision_id:
            raise GatewayError("RISK_BLOCKED", "the risk verdict belongs to a different decision")
        allowed = payload.get("allowed") or {}
        allowed_quantity = _decimal_or_none(allowed.get("quantity"))
        if allowed_quantity is None or intent.quantity > allowed_quantity:
            raise GatewayError("RISK_BLOCKED", "the intent exceeds the verdict's allowed quantity")
        if intent.position_id is None:
            raise GatewayError("RISK_BLOCKED", "an exit intent requires a position id")
        position = self._store.get_position(intent.position_id)
        if position is None:
            raise GatewayError("RISK_BLOCKED", "the position to close does not exist")
        position_payload = json.loads(position.payload_json)
        if position.state != "OPEN":
            raise GatewayError("RISK_BLOCKED", "the position is not OPEN")
        if position.symbol != intent.symbol:
            raise GatewayError("RISK_BLOCKED", "the intent symbol does not match the position")
        if position_payload.get("side") != intent.position_side.value:
            raise GatewayError("RISK_BLOCKED", "the intent side does not match the position")
        source_trigger = position_payload.get("source_trigger_id")
        if not source_trigger:
            raise GatewayError(
                "DATA_UNAVAILABLE",
                "the position has no source trigger; refusing to fabricate one",
            )
        if str(source_trigger) != record.trigger_id:
            raise GatewayError(
                "RISK_BLOCKED", "the risk verdict does not belong to the position's trigger"
            )
        self._require_recorded_mark(position_payload, mark_price)

    @staticmethod
    def _require_recorded_mark(position_payload: dict[str, object], mark_price: Decimal) -> None:
        """Refuse a close whose mark is not exactly the position's recorded mark."""
        raw_mark = position_payload.get("mark_price")
        if raw_mark is None:
            raise GatewayError("RISK_BLOCKED", "the position has no recorded mark")
        try:
            recorded = Decimal(str(raw_mark))
        except ArithmeticError as exc:
            raise GatewayError("RISK_BLOCKED", "the position mark is unreadable") from exc
        if not recorded.is_finite() or recorded != mark_price:
            raise GatewayError(
                "RISK_BLOCKED",
                "the submitted mark does not match the position's recorded mark",
            )

    def _bind_to_verdict(
        self,
        intent: OrderIntent,
        record: RiskVerdictRecord,
        *,
        mark_price: Decimal,
    ) -> None:
        """Refuse an intent that is not the one the persisted verdict approved.

        The gateway is the last line of defense (ADR-0006): it must not trust
        its caller to have used ``verdict.allowed_quantity`` or the same inputs.
        """
        payload = json.loads(record.payload_json)
        if record.decision_id != intent.decision_id:
            raise GatewayError("RISK_BLOCKED", "the risk verdict belongs to a different decision")

        allowed = payload.get("allowed") or {}
        allowed_quantity = _decimal_or_none(allowed.get("quantity"))
        if allowed_quantity is None or intent.quantity > allowed_quantity:
            raise GatewayError("RISK_BLOCKED", "the intent exceeds the verdict's allowed quantity")
        allowed_leverage = allowed.get("leverage")
        if allowed_leverage is None or intent.leverage > float(allowed_leverage):
            raise GatewayError("RISK_BLOCKED", "the intent exceeds the verdict's allowed leverage")

        trigger = self._store.get_trigger_event(record.trigger_id)
        if trigger is None:
            raise GatewayError("RISK_BLOCKED", "the risk verdict's trigger is missing")
        trigger_payload = json.loads(trigger.payload_json)
        if trigger_payload.get("symbol") != intent.symbol:
            raise GatewayError("RISK_BLOCKED", "the intent symbol does not match the verdict")
        if trigger_payload.get("side") != intent.position_side.value:
            raise GatewayError("RISK_BLOCKED", "the intent side does not match the verdict")

        requested = payload.get("requested") or {}
        try:
            request = RiskRequest(
                symbol=intent.symbol,
                side=intent.position_side.value,
                entry_price=Decimal(str(requested["entry_price"])),
                stop_price=_decimal_or_none(requested.get("stop_price")),
                quantity=None,
                risk_pct=float(requested["risk_pct"]),
                leverage=float(requested["leverage"]),
                notional_usd=_decimal_or_none(requested.get("notional_usd")),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise GatewayError("RISK_BLOCKED", "the risk verdict inputs are incomplete") from exc
        account = self._broker.account()
        positions = self._store.list_positions(state="OPEN", limit=None)
        equity, _view = compute_equity(
            Decimal(account.balance),
            [json.loads(record.payload_json) for record in positions],
        )
        digest = verdict_inputs_hash(
            request,
            equity=equity,
            open_positions=len(positions),
            spread_bps=record.spread_bps,
            expected_slippage_bps=record.expected_slippage_bps,
            daily_loss_usd=_decimal_or_zero(record.daily_loss_usd),
        )
        if digest != payload.get("inputs_hash"):
            raise GatewayError(
                "RISK_BLOCKED", "the risk verdict inputs changed since it was computed"
            )

        # The verdict's quantity/leverage caps do not bind the stop: a narrower
        # allowed quantity with a much wider stop could exceed the per-trade risk
        # the verdict approved, and the broker persists intent.stop_price. Bind
        # both the stop and the declared risk amount to the verdict.
        if intent.stop_price != request.stop_price:
            raise GatewayError("RISK_BLOCKED", "the intent stop does not match the verdict")
        allowed_risk = _decimal_or_none(allowed.get("risk_amount_usd"))
        # Fail closed: a verdict that does not carry an approved risk amount
        # cannot authorize a stop-loss exposure of any size.
        if allowed_risk is None:
            raise GatewayError("RISK_BLOCKED", "the verdict declares no allowed risk")
        if intent.risk_amount_usd > allowed_risk:
            raise GatewayError("RISK_BLOCKED", "the intent exceeds the verdict's allowed risk")

        # The caller's declared risk is not trustworthy (ADR-0006): recompute the
        # stop-loss risk the intent would actually carry at the mark it submits.
        # A far mark with the verdict's stop and quantity can otherwise exceed
        # the approved risk even though every cap above still passes.
        if request.stop_price is None or request.stop_price <= 0:
            raise GatewayError("RISK_BLOCKED", "the intent has no protective stop")
        if mark_price <= 0:
            raise GatewayError("RISK_BLOCKED", "the mark price is missing")
        actual_risk = intent.quantity * abs(mark_price - request.stop_price)
        if actual_risk > allowed_risk:
            raise GatewayError(
                "RISK_BLOCKED",
                "the submitted mark price exceeds the verdict's allowed risk",
            )
