"""Deterministic ADD risk gate for position management (slice 25).

A management ``ADD`` is the only risk-increasing action, so it is evaluated
twice: an evaluation verdict computes the clamped size and records the binding
cap, and an execution verdict re-evaluates the already step-rounded quantity the
order will actually submit. Both verdicts are persisted under distinct ids (the
decision id and a phase marker are part of the digest) so the evaluation row an
operator reads is never overwritten by the execution row an order references.

The LLM's advisory ``add_risk_pct`` is never authority: every cap comes from
:func:`oft.domain.risk.evaluate_risk`, and the execution phase refuses when the
re-evaluation disagrees with the clamped size instead of forcing an ``ALLOW``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from oft.application.kill_switch import record_audit
from oft.domain.oms import Position
from oft.domain.risk import (
    BindingCap,
    ManagementAction,
    RiskCaps,
    RiskRequest,
    RiskVerdict,
    Verdict,
    evaluate_risk,
    verdict_inputs_hash,
    verdict_to_contract,
)
from oft.domain.symbols import SymbolContract
from oft.ports.store import RiskVerdictRecord, StorePort

_EIGHT_DP = Decimal("0.00000001")


class AddGateError(Exception):
    """A typed ADD risk-gate refusal, translated to ``ManagementRejected``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AddGateInputs:
    """One immutable snapshot of the ADD gate's code-owned inputs."""

    store: StorePort
    decision_id: str
    position: Position
    mark: Decimal
    mark_as_of: str
    now: str
    caps: RiskCaps
    contract: SymbolContract
    equity: Decimal
    open_positions: int
    stale_inputs: tuple[str, ...]
    requested_quantity: Decimal | None
    requested_pct: float


@dataclass(frozen=True)
class AddEvaluation:
    """The pre-execution verdict and the id it is persisted under."""

    verdict: RiskVerdict
    payload: dict[str, Any]
    risk_verdict_id: str


@dataclass(frozen=True)
class AddExecution:
    """The execution verdict an ADD order binds to, plus its durable id."""

    payload: dict[str, Any]
    risk_verdict_id: str
    allowed_quantity: Decimal
    binding_cap: BindingCap
    requested_quantity: Decimal | None

    def to_view(self) -> dict[str, Any]:
        """The API/CLI view: the contract payload plus the requested/allowed pair."""
        view = dict(self.payload)
        view["requested_quantity"] = (
            None if self.requested_quantity is None else str(self.requested_quantity)
        )
        view["allowed_quantity"] = str(self.allowed_quantity)
        view["binding_cap"] = self.binding_cap.value
        view["risk_verdict_id"] = self.risk_verdict_id
        return view


def _requested_payload(gate: AddGateInputs) -> dict[str, Any]:
    stop_price = gate.position.stop_price
    return {
        "entry_price": str(gate.mark),
        "stop_price": "0" if stop_price is None else str(stop_price),
        "quantity": (None if gate.requested_quantity is None else str(gate.requested_quantity)),
        "risk_pct": gate.requested_pct,
        "leverage": gate.position.leverage,
        "notional_usd": None,
    }


def _risk_verdict_id(gate: AddGateInputs, quantity: Decimal | None, phase: str) -> str:
    """A deterministic verdict id unique per decision and phase.

    The decision id and phase are part of the digest so two decisions on the same
    position at the same mark and size never upsert one another's row, and the
    evaluation verdict survives alongside the execution verdict.
    """
    digest = hashlib.sha256(
        (f"{gate.position.position_id}:{gate.decision_id}:{phase}:{gate.mark}:{quantity}").encode()
    ).hexdigest()[:16]
    return f"RISK-MGD-{digest.upper()}"


def _evaluate(gate: AddGateInputs, *, quantity: Decimal | None) -> RiskVerdict:
    stop_price = gate.position.stop_price
    return evaluate_risk(
        request=RiskRequest(
            symbol=gate.position.symbol,
            side=gate.position.side.value,
            entry_price=gate.mark,
            stop_price=stop_price,
            quantity=quantity,
            risk_pct=gate.requested_pct,
            leverage=gate.position.leverage,
            notional_usd=None,
        ),
        caps=gate.caps,
        equity=gate.equity,
        open_positions=gate.open_positions,
        daily_loss_usd=None,
        mark_price=gate.mark,
        mark_as_of=gate.mark_as_of,
        now=gate.now,
        tick_size=gate.contract.tick_size,
        step_size=gate.contract.step_size,
        min_notional=gate.contract.min_notional,
        spread_bps=None,
        expected_slippage_bps=None,
        equity_stale_inputs=gate.stale_inputs,
    )


def _save_verdict(
    gate: AddGateInputs,
    *,
    risk_verdict_id: str,
    verdict: RiskVerdict,
    payload: dict[str, Any],
    binding_cap: BindingCap,
) -> None:
    gate.store.save_risk_verdict(
        RiskVerdictRecord(
            risk_verdict_id=risk_verdict_id,
            trigger_id=str(gate.position.source_trigger_id),
            decision_id=gate.decision_id,
            verdict=verdict.verdict.value,
            binding_cap=binding_cap.value,
            computed_at=verdict.computed_at,
            expires_at=verdict.expires_at,
            payload_json=json.dumps(payload, sort_keys=True),
            spread_bps=None,
            expected_slippage_bps=None,
            daily_loss_usd=None,
        )
    )


def evaluate_add(gate: AddGateInputs) -> AddEvaluation:
    """Compute and persist the pre-execution verdict (the requested vs allowed)."""
    try:
        verdict = _evaluate(gate, quantity=gate.requested_quantity)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise AddGateError("ORDER_REJECTED", f"the add cannot be evaluated: {exc}") from exc
    risk_verdict_id = _risk_verdict_id(gate, gate.requested_quantity, "evaluation")
    payload = verdict_to_contract(verdict, requested_payload=_requested_payload(gate))
    _save_verdict(
        gate,
        risk_verdict_id=risk_verdict_id,
        verdict=verdict,
        payload=payload,
        binding_cap=verdict.binding_cap,
    )
    record_audit(
        gate.store,
        event_type="RISK_VERDICT",
        actor={"kind": "SYSTEM", "id": "risk_gate"},
        subject={"kind": "RISK_VERDICT", "id": risk_verdict_id},
        payload={
            "verdict": verdict.verdict.value,
            "binding_cap": verdict.binding_cap.value,
            "purpose": ManagementAction.ADD.value,
            "phase": "evaluation",
            "requested_quantity": (
                None if gate.requested_quantity is None else str(gate.requested_quantity)
            ),
            "allowed_quantity": (
                None if verdict.allowed_quantity is None else str(verdict.allowed_quantity)
            ),
        },
        at=gate.now,
    )
    return AddEvaluation(verdict=verdict, payload=payload, risk_verdict_id=risk_verdict_id)


def persist_add_execution_verdict(
    gate: AddGateInputs,
    *,
    allowed_quantity: Decimal,
    binding_cap: BindingCap,
) -> AddExecution:
    """Re-evaluate the clamped quantity and persist the verdict the order binds to.

    The re-evaluation must agree with the clamp: a ``BLOCK``/``REDUCE`` or a
    different allowed quantity means the gate no longer authorizes this add, so
    the request is refused instead of rewriting the answer to ``ALLOW``.
    """
    try:
        execution = _evaluate(gate, quantity=allowed_quantity)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise AddGateError("ORDER_REJECTED", f"the add cannot be evaluated: {exc}") from exc
    if execution.verdict is not Verdict.ALLOW:
        raise AddGateError(
            "RISK_BLOCKED",
            "the execution re-evaluation did not authorize the add "
            f"({execution.verdict.value}, binding cap {execution.binding_cap.value})",
        )
    if execution.allowed_quantity != allowed_quantity:
        raise AddGateError(
            "RISK_BLOCKED", "the execution re-evaluation reduced the clamped add size"
        )
    stop_price = gate.position.stop_price
    if stop_price is None:
        raise AddGateError("RISK_BLOCKED", "the add has no protective stop")
    actual_risk = abs(gate.mark - stop_price) * allowed_quantity
    risk_cap = actual_risk.quantize(_EIGHT_DP, rounding=ROUND_UP)
    payload = verdict_to_contract(execution, requested_payload=_requested_payload(gate))
    # Derive the persisted verdict from the execution evaluation. Only the
    # operator-facing binding cap and the clamped size/risk override the raw
    # verdict, and the leverage is the clamped value the gateway will compare.
    payload["verdict"] = execution.verdict.value
    payload["binding_cap"] = binding_cap.value
    payload["allowed"]["quantity"] = str(allowed_quantity)
    payload["allowed"]["leverage"] = execution.allowed_leverage
    payload["allowed"]["risk_amount_usd"] = str(risk_cap)
    payload["allowed"]["notional_usd"] = (
        None if execution.allowed_notional_usd is None else str(execution.allowed_notional_usd)
    )
    payload["inputs_hash"] = verdict_inputs_hash(
        RiskRequest(
            symbol=gate.position.symbol,
            side=gate.position.side.value,
            entry_price=gate.mark,
            stop_price=stop_price,
            quantity=None,
            risk_pct=gate.requested_pct,
            leverage=gate.position.leverage,
            notional_usd=None,
        ),
        equity=gate.equity,
        open_positions=gate.open_positions,
        spread_bps=None,
        expected_slippage_bps=None,
        daily_loss_usd=None,
    )
    risk_verdict_id = _risk_verdict_id(gate, allowed_quantity, "execution")
    _save_verdict(
        gate,
        risk_verdict_id=risk_verdict_id,
        verdict=execution,
        payload=payload,
        binding_cap=binding_cap,
    )
    record_audit(
        gate.store,
        event_type="RISK_VERDICT",
        actor={"kind": "SYSTEM", "id": "risk_gate"},
        subject={"kind": "RISK_VERDICT", "id": risk_verdict_id},
        payload={
            "verdict": execution.verdict.value,
            "binding_cap": binding_cap.value,
            "purpose": ManagementAction.ADD.value,
            "phase": "execution",
            "requested_quantity": (
                None if gate.requested_quantity is None else str(gate.requested_quantity)
            ),
            "allowed_quantity": str(allowed_quantity),
        },
        at=gate.now,
    )
    return AddExecution(
        payload=payload,
        risk_verdict_id=risk_verdict_id,
        allowed_quantity=allowed_quantity,
        binding_cap=binding_cap,
        requested_quantity=gate.requested_quantity,
    )


__all__ = [
    "AddEvaluation",
    "AddExecution",
    "AddGateError",
    "AddGateInputs",
    "evaluate_add",
    "persist_add_execution_verdict",
]
