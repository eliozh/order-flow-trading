"""The sole submit path: kill switch, risk verdict, human approval, idempotency.

No API/CLI route may call a broker directly. The gateway is the only writer of
orders and it refuses an intent without a persisted, fresh, non-blocking risk
verdict and a human approval (ADR-0006).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from oft.application.kill_switch import KillSwitch, record_audit
from oft.application.paper_broker import BrokerResult, OrderIntent, PaperBroker
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort


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

        result = self._broker.submit(
            intent,
            mark_price=mark_price,
            mark_as_of=mark_as_of,
            now=now,
        )
        self._audit(
            event_type="ORDER_FILLED",
            actor=auth,
            subject={"kind": "ORDER", "id": result.order.order_id},
            payload={
                "client_order_id": result.order.client_order_id,
                "status": result.order.status.value,
                "fill_id": result.fill.fill_id,
                "position_id": result.position.position_id,
                "environment": self._environment,
            },
            at=now,
        )
        return result

    def _audit(
        self,
        *,
        event_type: str,
        actor: AuthContext,
        subject: dict[str, Any],
        payload: dict[str, Any],
        at: str,
    ) -> None:
        record_audit(
            self._store,
            event_type=event_type,
            actor={"kind": actor.kind, "id": actor.actor_id},
            subject=subject,
            payload=payload,
            at=at,
        )
