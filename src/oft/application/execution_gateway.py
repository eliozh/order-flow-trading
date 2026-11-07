"""The sole submit path: kill switch, risk verdict, human approval, idempotency.

No API/CLI route may call a broker directly. The gateway is the only writer of
orders and it refuses an intent without a persisted, fresh, non-blocking risk
verdict and a human approval (ADR-0006).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from oft.application.kill_switch import KillSwitch
from oft.application.paper_broker import BrokerResult, OrderIntent, PaperBroker
from oft.domain.risk import RiskRequest, verdict_inputs_hash
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import RiskVerdictRecord, StorePort


def _decimal_or_none(value: object) -> Decimal | None:
    """Parse an exact decimal; ``None`` and the contract's "0" mean absent."""
    if value is None or value == "0":
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
        self._kill_switch.require_clear()
        if auth.approval_id is None:
            raise GatewayError("ORDER_REJECTED", "human paper approval is required")
        verdict = self._store.get_risk_verdict(intent.risk_verdict_id)
        if verdict is None:
            raise GatewayError("RISK_BLOCKED", "a persisted risk verdict is required")
        if verdict.verdict == "BLOCK":
            raise GatewayError("RISK_BLOCKED", "the risk verdict blocks this intent")
        now = utc_timestamp(self._clock.now())
        if verdict.expires_at <= now:
            raise GatewayError("RISK_BLOCKED", "the risk verdict has expired")

        existing = self._store.get_order_by_idempotency(intent.idempotency_key)
        if existing is not None:
            payload = json.loads(existing.payload_json)
            raise GatewayError("ORDER_DUPLICATE", f"order already exists: {payload['order_id']}")

        self._bind_to_verdict(intent, verdict, mark_price=mark_price)

        result = self._broker.submit(
            intent,
            mark_price=mark_price,
            mark_as_of=mark_as_of,
            now=now,
            audit_actor={"kind": auth.kind, "id": auth.actor_id},
        )
        return result

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
        digest = verdict_inputs_hash(
            request,
            equity=Decimal(account.balance),
            open_positions=len(self._store.list_positions(state="OPEN")),
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
