"""Position-management decisions, deterministic validation, and the paper path.

For one OPEN position the service asks the ``MANAGEMENT`` role for a
``HOLD | PARTIAL_CLOSE | FULL_CLOSE | ADD`` recommendation, validates it in code
against the position state, classifies it for the kill-switch policy, and only
then executes it through the single gateway. The LLM never supplies an order id,
a risk cap, a quantity as authority, or an execution decision: every number is
recomputed here (or by the deterministic risk gate) and clamped to the stricter
cap. ``HOLD`` and every blocked/invalid proposal write no order.

Deliberate size exception: this module remains above the project's 800-line soft
ceiling because prompt construction, LLM provenance, the execution/replay result
shaping, and the decision dispatch are one cohesive orchestration unit. The
largest separable concern -- the deterministic ADD risk gate (evaluation,
clamp, and the execution verdict the order binds to) -- lives in
:mod:`oft.application.management_add` behind the small ``AddGateInputs``
parameter object. A future split should move the prompt/provenance block and the
replay block the same way, without changing behaviour.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from oft.application.budget_ledger import BudgetEntry, BudgetLedger
from oft.application.entry_service import RiskCapsError, effective_caps
from oft.application.kill_switch import KillSwitch, KillSwitchActive, record_audit
from oft.application.knowledge_service import KnowledgeService, knowledge_injection
from oft.application.llm_orchestrator import (
    LLMConfigurationError,
    LLMOrchestrator,
    LLMRuntimeRegistry,
    provider_configured,
)
from oft.application.llm_validation import (
    ValidationIssue,
    decision_envelope,
    hash_text,
    schema_text,
)
from oft.application.management_add import (
    AddGateError,
    AddGateInputs,
    evaluate_add,
    persist_add_execution_verdict,
)
from oft.application.paper_account import PaperAccountService, compute_equity
from oft.application.position_service import (
    ManagementMark,
    PositionService,
)
from oft.application.prompt_builder import KnowledgeInjection
from oft.application.prompt_service import (
    PromptService,
    RenderedPrompt,
    secret_values_from_settings,
)
from oft.config import Settings
from oft.domain.management import (
    ManagementSizingError,
    plan_partial_close,
    validate_position_management,
)
from oft.domain.oms import Position
from oft.domain.pnl import json_decimal
from oft.domain.prompts import PromptRole
from oft.domain.risk import (
    BindingCap,
    ManagementAction,
    ManagementClassification,
    Verdict,
    add_risk_quantity,
    classify_management_intent,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import (
    LLMAdapterError,
    LLMPort,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMUsage,
    sanitize_provider_id,
)
from oft.ports.store import (
    DecisionRecord,
    LLMCallRecord,
    StorePort,
)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


class ManagementRejected(Exception):
    """A typed management failure; ``code`` maps to the API error catalog."""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class ManagementResult:
    """The outcome of one ``oft pos manage`` / API management call."""

    position_id: str
    decision_id: str
    action: str
    status: str
    executed: bool
    reason_code: str
    reason: str | None = None
    llm: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    trade_log: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    idempotent_replay: bool = False

    def to_contract(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "decision_id": self.decision_id,
            "action": self.action,
            "status": self.status,
            "executed": self.executed,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "llm": self.llm,
            "risk": self.risk,
            "order": self.order,
            "fill": self.fill,
            "position": self.position,
            "trade_log": self.trade_log,
            "account": self.account,
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class _PositionSource:
    """The trigger/opportunity/report chain resolved once for prompt and subject."""

    timeframe: str
    scan_id: str | None
    report_id: str | None
    opportunity_id: str | None
    trigger_id: str | None


def _total_usage(calls: Sequence[LLMCallRecord]) -> dict[str, int]:
    totals = dict.fromkeys(_USAGE_FIELDS, 0)
    for call in calls:
        try:
            payload = json.loads(call.usage_json or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in _USAGE_FIELDS:
            value = payload.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field] += value
    return LLMUsage(**totals).to_contract()


def _total_cost(budgets: list[BudgetEntry]) -> tuple[str, str, bool]:
    if not budgets:
        return "0", "stub/v1", True
    amount = Decimal("0")
    price_version = "stub/v1"
    unpriced = False
    for entry in budgets:
        payload = entry.payload
        try:
            amount += Decimal(str(payload.get("cost_usd", "0")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        price_version = str(payload.get("price_version") or price_version)
        unpriced = unpriced or bool(payload.get("unpriced", True))
    return format(amount, "f"), price_version, unpriced


_LLM_ERROR_CODES = frozenset({"LLM_REFUSED", "LLM_OUTPUT_TRUNCATED", "LLM_SCHEMA_INVALID"})


def _llm_error_code(code: str | None) -> str:
    """Map a validator code to the error catalog's LLM vocabulary."""
    return code if code in _LLM_ERROR_CODES else "LLM_SCHEMA_INVALID"


class ManagementService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: LLMPort | None,
        settings: Settings,
        kill_switch: KillSwitch,
        runtime: LLMRuntimeRegistry | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._provider = provider
        self._settings = settings
        self._kill_switch = kill_switch
        self._budget = BudgetLedger(store=store, settings=settings)
        self._orchestrator = (
            LLMOrchestrator(
                store=store,
                clock=clock,
                provider=provider,
                settings=settings,
                budget=self._budget,
                runtime=runtime,
            )
            if provider is not None
            else None
        )
        self._positions = PositionService(store=store, clock=clock, settings=settings)
        self._paper = PaperAccountService(settings=settings, store=store, clock=clock)
        self._secrets = secret_values_from_settings(settings)
        self._prompts = PromptService(store=store, clock=clock, secret_values=self._secrets)
        self._knowledge = KnowledgeService(settings=settings, clock=clock)

    # -- entry point -----------------------------------------------------

    def manage(
        self,
        position_id: str,
        *,
        idempotency_key: str,
        provider_name: str | None = None,
        actor_id: str = "cli",
    ) -> ManagementResult:
        key = (idempotency_key or "").strip()
        if not key:
            raise ManagementRejected(
                "IDEMPOTENCY_KEY_REQUIRED", "a management action requires an Idempotency-Key"
            )
        decision_id = _decision_id(position_id, key)
        replay = self._replay(position_id, decision_id, provider_name=provider_name)
        if replay is not None:
            return replay

        context = self._positions.management_mark(position_id)
        self._provider_name(provider_name)
        source = self._source_context(context.position)
        knowledge = self._knowledge_injection()
        rendered, request = self._request(context, source.timeframe, knowledge)

        orchestrator = self._orchestrator
        assert orchestrator is not None
        try:
            orchestration = orchestrator.complete_with_validation(
                request=request,
                decision_id=decision_id,
                role=LLMRole.MANAGEMENT,
            )
        except (LLMConfigurationError, LLMAdapterError) as exc:
            raise ManagementRejected(exc.code, exc.message) from exc
        outcome = orchestration.outcome
        final = orchestration.final_result
        calls = list(orchestration.calls)
        budgets = list(orchestration.budgets)
        provenance = self._provenance(
            final, rendered, request.input_hash, calls, budgets, context.now, knowledge
        )
        subject = self._subject(context, source)

        if not outcome.valid:
            errors = outcome.errors
            envelope = decision_envelope(
                decision_id=decision_id,
                role=LLMRole.MANAGEMENT.value,
                output=None,
                subject=subject,
                provenance=provenance,
                validation_status="REJECTED",
                validation_errors=errors,
                rejected_reason=(
                    f"{errors[0].code}: {errors[0].message}" if errors else "invalid output"
                ),
                raw_output_hash=outcome.raw_output_hash,
                persisted_at=context.now,
                decision_type="POSITION_MANAGEMENT",
            )
            self._save_decision(
                decision_id, "REJECTED", outcome.raw_output_hash, envelope, context.now
            )
            first = errors[0] if errors else None
            raise ManagementRejected(
                _llm_error_code(first.code if first is not None else None),
                first.message if first is not None else "the management output was rejected",
                field=first.field if first is not None else None,
            )

        output = outcome.output
        assert output is not None  # ``valid`` guarantees a parsed output
        management = output.model_dump(mode="json").get("position_management") or {}
        issues = validate_position_management(management, context.position, mark_price=context.mark)
        if issues:
            envelope = decision_envelope(
                decision_id=decision_id,
                role=LLMRole.MANAGEMENT.value,
                output=outcome.output,
                subject=subject,
                provenance=provenance,
                validation_status="REJECTED",
                validation_errors=tuple(
                    ValidationIssue(issue.field, issue.code, issue.message) for issue in issues
                ),
                rejected_reason=f"{issues[0].field}: {issues[0].message}",
                raw_output_hash=outcome.raw_output_hash,
                persisted_at=context.now,
                decision_type="POSITION_MANAGEMENT",
            )
            self._save_decision(
                decision_id, "REJECTED", outcome.raw_output_hash, envelope, context.now
            )
            raise ManagementRejected(
                "LLM_SCHEMA_INVALID",
                f"{issues[0].field}: {issues[0].message}",
                field=issues[0].field,
            )

        action = str(management["action"])
        envelope = decision_envelope(
            decision_id=decision_id,
            role=LLMRole.MANAGEMENT.value,
            output=outcome.output,
            subject=subject,
            provenance=provenance,
            validation_status=outcome.status,
            validation_errors=outcome.errors,
            rejected_reason=None,
            raw_output_hash=outcome.raw_output_hash,
            persisted_at=context.now,
            decision_type="POSITION_MANAGEMENT",
        )
        self._save_decision(
            decision_id, outcome.status, outcome.raw_output_hash, envelope, context.now
        )
        llm_view = {
            "decision_id": decision_id,
            "action": action,
            "validation_status": outcome.status,
            "confidence": envelope.get("confidence"),
            "rationale": envelope.get("rationale"),
            "urgency": management.get("urgency"),
            "provenance": {
                "provider": provenance["provider"],
                "model": provenance["model"],
                "prompt_version": provenance["prompt_version"],
                "knowledge_artifact_hash": provenance["knowledge_artifact_hash"],
                "cost": provenance["cost"],
            },
        }

        classification = classify_management_intent(
            action=action,
            position_side=context.position.side.value,
            current_stop=context.position.stop_price,
            new_stop_price=json_decimal(management.get("new_stop_price")),
        )
        self._audit_classification(context, decision_id, action, classification)
        if classification.risk_increasing:
            try:
                self._kill_switch.require_clear()
            except KillSwitchActive as exc:
                raise ManagementRejected(exc.code, exc.message) from exc

        if action == ManagementAction.HOLD.value:
            position_payload = self._positions.record_management_hold(
                position_id,
                decision_id=decision_id,
                note=_note(management),
                now=context.now,
            )
            return self._result(
                context,
                decision_id=decision_id,
                action=action,
                status="NO_ACTION",
                executed=False,
                reason_code="HOLD",
                reason="the management decision was HOLD; no order was created",
                llm=llm_view,
                position=position_payload,
            )

        if action == ManagementAction.FULL_CLOSE.value:
            return self._full_close(context, decision_id=decision_id, llm=llm_view, reason=None)

        if action == ManagementAction.PARTIAL_CLOSE.value:
            return self._partial_close(
                context, decision_id=decision_id, management=management, llm=llm_view
            )

        if action == ManagementAction.ADD.value:
            return self._add(context, decision_id=decision_id, management=management, llm=llm_view)

        # The action vocabulary is closed and already validated above; a value
        # that reaches here is a single-repair bug, never an implicit ADD.
        raise ManagementRejected("LLM_SCHEMA_INVALID", f"unsupported management action {action!r}")

    # -- execution paths -------------------------------------------------

    def _full_close(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        llm: dict[str, Any],
        reason: str | None,
    ) -> ManagementResult:
        position = context.position
        purpose = ManagementAction.FULL_CLOSE.value
        risk_verdict_id = self._positions.persist_management_exit_verdict(
            position=position,
            decision_id=decision_id,
            quantity=position.size,
            mark=context.mark,
            now=context.now,
            purpose=purpose,
        )
        approval_id = _approval_id(decision_id, purpose)
        result = self._positions.prepare_management_exit(
            context,
            decision_id=decision_id,
            purpose=purpose,
            quantity=position.size,
            idempotency_key=_order_key(decision_id, purpose),
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            note=reason or "management full close",
        )
        trade = self._store.get_trade_log_for_position(position.position_id)
        if trade is None:
            raise ManagementRejected("CONFLICT", "the close committed but no trade log was written")
        trade_payload = json.loads(trade.payload_json)
        position_payload = self._read_position_payload(position.position_id)
        return self._result(
            context,
            decision_id=decision_id,
            action=purpose,
            status="EXECUTED",
            executed=True,
            reason_code="FULL_CLOSE",
            reason=reason or "the position was closed by the management decision",
            llm=llm,
            risk=self._risk_view(risk_verdict_id),
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
            trade_log=trade_payload,
        )

    def _partial_close(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        management: dict[str, Any],
        llm: dict[str, Any],
    ) -> ManagementResult:
        position = context.position
        contract = self._store.get_symbol(position.symbol)
        if contract is None:
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                f"the symbol contract for {position.symbol} is missing; "
                "the partial-close size cannot be normalized",
            )
        fraction = json_decimal(management.get("close_fraction"))
        if fraction is None:
            raise ManagementRejected(
                "LLM_SCHEMA_INVALID", "position_management.close_fraction is required"
            )
        try:
            plan = plan_partial_close(
                size=position.size,
                fraction=fraction,
                mark_price=context.mark,
                step_size=contract.step_size,
                min_notional=contract.min_notional,
            )
        except ManagementSizingError as exc:
            raise ManagementRejected("VALIDATION_FAILED", exc.message) from exc
        if plan.escalated:
            return self._full_close(
                context,
                decision_id=decision_id,
                llm=llm,
                reason=plan.reason,
            )
        purpose = ManagementAction.PARTIAL_CLOSE.value
        risk_verdict_id = self._positions.persist_management_exit_verdict(
            position=position,
            decision_id=decision_id,
            quantity=plan.quantity,
            mark=context.mark,
            now=context.now,
            purpose=purpose,
        )
        approval_id = _approval_id(decision_id, purpose)
        result = self._positions.prepare_management_exit(
            context,
            decision_id=decision_id,
            purpose=purpose,
            quantity=plan.quantity,
            idempotency_key=_order_key(decision_id, purpose),
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            note=_note(management),
        )
        self._apply_stop_targets(context, decision_id=decision_id, management=management)
        position_payload = self._read_position_payload(position.position_id)
        return self._result(
            context,
            decision_id=decision_id,
            action=purpose,
            status="EXECUTED",
            executed=True,
            reason_code="PARTIAL_CLOSE",
            reason="the position was partially reduced by the management decision",
            llm=llm,
            risk=self._risk_view(risk_verdict_id),
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
        )

    def _add(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        management: dict[str, Any],
        llm: dict[str, Any],
    ) -> ManagementResult:
        add_risk_pct = management.get("add_risk_pct")
        try:
            requested_pct = float(add_risk_pct) if add_risk_pct is not None else 0.0
        except (TypeError, ValueError) as exc:
            raise ManagementRejected("LLM_SCHEMA_INVALID", "add_risk_pct is not a number") from exc
        try:
            caps, _source = effective_caps(self._settings, self._store)
        except RiskCapsError as exc:
            raise ManagementRejected("VALIDATION_FAILED", exc.message) from exc
        contract = self._store.get_symbol(context.position.symbol)
        if contract is None:
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                f"the symbol contract for {context.position.symbol} is missing; "
                "the add size cannot be normalized",
            )
        # Persist the mark before measuring equity: the gateway re-reads the
        # position payload when it re-derives the verdict digest, so both sides
        # must value the account at the same recorded mark.
        context = self._positions.persist_management_mark(context)
        position = context.position
        equity, open_positions, stale_inputs = self._gate_inputs()
        stop_price = position.stop_price
        requested_quantity = (
            None
            if stop_price is None
            else add_risk_quantity(
                equity=equity,
                add_risk_pct=requested_pct,
                mark_price=context.mark,
                stop_price=stop_price,
            )
        )
        gate = AddGateInputs(
            store=self._store,
            decision_id=decision_id,
            position=position,
            mark=context.mark,
            mark_as_of=context.mark_as_of,
            now=context.now,
            caps=caps,
            contract=contract,
            equity=equity,
            open_positions=open_positions,
            stale_inputs=stale_inputs,
            requested_quantity=requested_quantity,
            requested_pct=requested_pct,
        )
        try:
            evaluated = evaluate_add(gate)
        except AddGateError as exc:
            raise ManagementRejected(exc.code, exc.message) from exc
        pre_verdict = evaluated.verdict
        # An ADD is risk-increasing: refuse whenever the position's leverage is
        # above the configured maximum, independent of which quantity cap binds.
        # Otherwise a tightened leverage cap could be bypassed by a MAX_NOTIONAL
        # or PER_TRADE_RISK quantity clamp that happens to bind first.
        over_leverage = position.leverage > caps.max_leverage
        if pre_verdict.verdict is Verdict.BLOCK or over_leverage:
            binding_cap = BindingCap.MAX_LEVERAGE if over_leverage else pre_verdict.binding_cap
            risk_payload = dict(evaluated.payload)
            risk_payload["binding_cap"] = binding_cap.value
            if over_leverage:
                risk_payload["reasons"] = [
                    *risk_payload.get("reasons", []),
                    "the position leverage exceeds the configured maximum",
                ]
            # The evaluation verdict was persisted before this override. Record
            # the refusal with the effective cap so the stored evidence names
            # the same binding cap the API/CLI response reports. An over-leverage
            # block is the case the evaluation alone cannot name, because the
            # quantity caps may bind first and report a different verdict.
            record_audit(
                self._store,
                event_type="MANAGEMENT_BLOCKED",
                actor={"kind": "SYSTEM", "id": "management_service"},
                subject={"kind": "POSITION", "id": position.position_id},
                payload={
                    "decision_id": decision_id,
                    "action": ManagementAction.ADD.value,
                    "reason_code": "RISK_BLOCKED",
                    "binding_cap": binding_cap.value,
                    "verdict": pre_verdict.verdict.value,
                    "risk_verdict_id": evaluated.risk_verdict_id,
                    "reasons": list(risk_payload.get("reasons", [])),
                },
                at=context.now,
            )
            return self._result(
                context,
                decision_id=decision_id,
                action=ManagementAction.ADD.value,
                status="BLOCKED",
                executed=False,
                reason_code="RISK_BLOCKED",
                reason=f"the risk gate blocked the add; the binding cap is {binding_cap.value}",
                llm=llm,
                risk=risk_payload,
            )
        allowed_quantity = pre_verdict.allowed_quantity
        if allowed_quantity is None or allowed_quantity <= 0:
            raise ManagementRejected("RISK_BLOCKED", "the risk gate allowed no add size")
        try:
            execution = persist_add_execution_verdict(
                gate,
                allowed_quantity=allowed_quantity,
                binding_cap=pre_verdict.binding_cap,
            )
        except AddGateError as exc:
            raise ManagementRejected(exc.code, exc.message) from exc
        approval_id = _approval_id(decision_id, ManagementAction.ADD.value)
        result = self._positions.prepare_management_add(
            context,
            decision_id=decision_id,
            quantity=allowed_quantity,
            idempotency_key=_order_key(decision_id, ManagementAction.ADD.value),
            risk_verdict_id=execution.risk_verdict_id,
            approval_id=approval_id,
            stop_price=stop_price or Decimal("0"),
            leverage=position.leverage,
            note=_note(management),
        )
        self._apply_stop_targets(context, decision_id=decision_id, management=management)
        position_payload = self._read_position_payload(position.position_id)
        clamped = requested_quantity is not None and requested_quantity != allowed_quantity
        return self._result(
            context,
            decision_id=decision_id,
            action=ManagementAction.ADD.value,
            status="EXECUTED",
            executed=True,
            reason_code="ADD",
            reason=(
                f"the add was clamped to the stricter cap ({execution.binding_cap.value})"
                if clamped
                else "the add was executed within every cap"
            ),
            llm=llm,
            risk=execution.to_view(),
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
        )

    def _apply_stop_targets(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        management: dict[str, Any],
    ) -> None:
        new_stop = json_decimal(management.get("new_stop_price"))
        raw_targets = management.get("new_targets") or []
        targets = (
            tuple(Decimal(str(target)) for target in raw_targets)
            if isinstance(raw_targets, list) and raw_targets
            else None
        )
        if new_stop is None and targets is None:
            return
        # ``None`` means "leave the stop unchanged": a target-only change must
        # never synthesize a placeholder stop (it is contract-invalid and would
        # be treated as "no stop" by the protective-exit tick).
        self._positions.apply_management_stop(
            context.position.position_id,
            stop_price=new_stop,
            target_prices=targets,
            decision_id=decision_id,
            now=context.now,
            reference=context.mark,
        )

    # -- risk gate -------------------------------------------------------

    def _gate_inputs(self) -> tuple[Decimal, int, tuple[str, ...]]:
        account = self._paper.ensure()
        positions = self._store.list_positions(state="OPEN", limit=None)
        equity, view = compute_equity(
            Decimal(account.balance),
            [json.loads(record.payload_json) for record in positions],
        )
        return equity, len(positions), view.stale_inputs

    # -- prompt / provenance ---------------------------------------------

    def _request(
        self, context: ManagementMark, timeframe: str, knowledge: KnowledgeInjection
    ) -> tuple[RenderedPrompt, LLMRequest]:
        rendered = self._prompts.render_active(
            PromptRole.MANAGEMENT,
            {
                "symbol": context.position.symbol,
                "timeframe": timeframe,
                "position": json.dumps(self._position_prompt(context), sort_keys=True),
                "schema": schema_text(),
                "knowledge": knowledge.text,
            },
            secret_values=self._secrets,
        )
        digest = hash_text(f"{rendered.system}\n{rendered.user}\n{schema_text()}")
        request = LLMRequest(
            role=LLMRole.MANAGEMENT,
            model="",
            system_prompt=rendered.system,
            user_prompt=rendered.user,
            schema_json=schema_text(),
            input_hash=digest,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_hash=rendered.prompt_hash,
            request_id=f"REQ-MGD-{digest[:12].upper()}",
            system_prompt_id=rendered.system_prompt_id,
            system_prompt_version=rendered.system_version,
            system_prompt_hash=rendered.system_prompt_hash,
        )
        return rendered, request

    def _position_prompt(self, context: ManagementMark) -> dict[str, Any]:
        position = context.position
        payload = context.payload
        history = payload.get("management_history")
        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side.value,
            "state": position.state,
            "size": str(position.size),
            "entry_price": str(position.entry_price),
            "avg_entry_price": str(position.avg_entry_price),
            "mark_price": str(context.mark),
            "stop_price": None if position.stop_price is None else str(position.stop_price),
            "target_prices": [str(price) for price in position.target_prices],
            "leverage": position.leverage,
            "notional_usd": str(position.notional_usd),
            "risk_amount_usd": str(position.risk_amount_usd),
            "risk_pct": position.risk_pct,
            "fees_usd": str(position.fees_usd),
            "funding_usd": str(position.funding_usd),
            "realized_pnl_usd": payload.get("realized_pnl_usd"),
            "opened_at": position.opened_at,
            "updated_at": position.updated_at,
            "management_history": history if isinstance(history, list) else [],
        }

    def _knowledge_injection(self) -> KnowledgeInjection:
        return knowledge_injection(self._knowledge, PromptRole.MANAGEMENT.value)

    def _source_context(self, position: Position) -> _PositionSource:
        """Resolve the trigger -> opportunity -> report/scan chain exactly once.

        Both the prompt's ``timeframe`` and the decision envelope's ``subject``
        are rendered from this one read, so they cannot disagree. The manual
        management path derives its decision id from the idempotency key (see
        :func:`_decision_id`); the monitor path uses the mark-derived
        :func:`oft.domain.positions.management_decision_id`. Both are single
        ``MGD-`` ids for their own trigger; this read never chooses between them.
        """
        trigger_id = position.source_trigger_id
        scan_id = report_id = opportunity_id = None
        timeframe: str | None = None
        trigger = self._store.get_trigger_event(trigger_id) if trigger_id else None
        if trigger is not None:
            opportunity_id = trigger.opportunity_id
            opportunity = self._store.get_opportunity(opportunity_id)
            if opportunity is not None:
                source = json.loads(opportunity.payload_json).get("source") or {}
                scan_id = source.get("scan_id")
                report_id = source.get("report_id")
                report = self._store.get_report(str(report_id)) if report_id else None
                if report is not None and report.timeframe:
                    timeframe = report.timeframe
                else:
                    scan = self._store.get_scan(str(scan_id)) if scan_id else None
                    if scan is not None and scan.period:
                        timeframe = scan.period
        if timeframe is None:
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                "the position has no source report timeframe for the management prompt",
            )
        return _PositionSource(
            timeframe=timeframe,
            scan_id=scan_id,
            report_id=report_id,
            opportunity_id=opportunity_id,
            trigger_id=trigger_id,
        )

    def _subject(self, context: ManagementMark, source: _PositionSource) -> dict[str, Any]:
        position = context.position
        return {
            "symbol": position.symbol,
            "timeframe": source.timeframe,
            "as_of": context.now,
            "scan_id": source.scan_id,
            "report_id": source.report_id,
            "opportunity_id": source.opportunity_id,
            "trigger_id": source.trigger_id,
            "position_id": position.position_id,
            "environment": position.environment,
        }

    def _provenance(
        self,
        result: LLMResult,
        rendered: RenderedPrompt,
        input_hash: str,
        calls: Sequence[LLMCallRecord],
        budgets: list[BudgetEntry],
        now: str,
        knowledge: KnowledgeInjection,
    ) -> dict[str, Any]:
        amount_usd, price_version, unpriced = _total_cost(budgets)
        return {
            "provider": result.provider,
            "model": result.model,
            "model_snapshot": None,
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.version,
            "prompt_hash": rendered.prompt_hash,
            "knowledge_artifact_hash": knowledge.artifact_id,
            "knowledge_status": knowledge.status,
            "knowledge_reason": knowledge.reason,
            "input_hash": input_hash,
            "schema_version": "1.0.0",
            "temperature": 0.0,
            "seed": None,
            "request_id": sanitize_provider_id(result.request_id, self._secrets),
            "latency_ms": result.latency_ms,
            "usage": _total_usage(calls),
            "cost": {
                "amount_usd": amount_usd,
                "currency": "USD",
                "price_version": price_version,
                "unpriced": unpriced,
            },
            "recorded_at": now,
        }

    # -- persistence helpers ---------------------------------------------

    def _save_decision(
        self,
        decision_id: str,
        validation_status: str,
        raw_output_hash: str,
        envelope: dict[str, Any],
        now: str,
    ) -> None:
        self._store.save_decision(
            DecisionRecord(
                decision_id=decision_id,
                role=LLMRole.MANAGEMENT.value,
                decision_type="POSITION_MANAGEMENT",
                validation_status=validation_status,
                raw_output_hash=raw_output_hash,
                persisted_at=now,
                payload_json=json.dumps(envelope, sort_keys=True),
            )
        )

    def _audit_classification(
        self,
        context: ManagementMark,
        decision_id: str,
        action: str,
        classification: ManagementClassification,
    ) -> None:
        record_audit(
            self._store,
            event_type="MANAGEMENT_CLASSIFIED",
            actor={"kind": "SYSTEM", "id": "management_service"},
            subject={"kind": "POSITION", "id": context.position.position_id},
            payload={
                "decision_id": decision_id,
                "action": action,
                "risk_increasing": classification.risk_increasing,
                "stop_change": classification.stop_change.value,
                "reasons": list(classification.reasons),
                # The pre-change snapshot makes the replay-reconciliation guard
                # checkable: a same-key replay may only re-apply this decision's
                # change while the row still carries these exact values. The
                # decision-time mark is kept so a recovered stop is validated
                # against the same reference the original decision used, not the
                # post-fill stored mark (which includes slippage).
                "position_before": {
                    "stop_price": (
                        None
                        if context.position.stop_price is None
                        else str(context.position.stop_price)
                    ),
                    "target_prices": [str(price) for price in context.position.target_prices],
                    "mark_price": str(context.mark),
                },
            },
            at=context.now,
        )

    def _risk_view(self, risk_verdict_id: str) -> dict[str, Any] | None:
        record = self._store.get_risk_verdict(risk_verdict_id)
        if record is None:
            return None
        payload: dict[str, Any] = json.loads(record.payload_json)
        payload.setdefault("verdict", record.verdict)
        payload.setdefault("binding_cap", record.binding_cap)
        return payload

    def _read_position_payload(self, position_id: str) -> dict[str, Any]:
        record = self._store.get_position(position_id)
        if record is None:
            raise ManagementRejected(
                "POSITION_NOT_FOUND", f"position {position_id} disappeared during the action"
            )
        payload: dict[str, Any] = json.loads(record.payload_json)
        return payload

    def _result(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        action: str,
        status: str,
        executed: bool,
        reason_code: str,
        reason: str | None,
        llm: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        fill: dict[str, Any] | None = None,
        position: dict[str, Any] | None = None,
        trade_log: dict[str, Any] | None = None,
        idempotent_replay: bool = False,
    ) -> ManagementResult:
        account = self._paper.summary()
        return ManagementResult(
            position_id=context.position.position_id,
            decision_id=decision_id,
            action=action,
            status=status,
            executed=executed,
            reason_code=reason_code,
            reason=reason,
            llm=llm,
            risk=risk,
            order=order,
            fill=fill,
            position=position,
            trade_log=trade_log,
            account={
                "account_id": account["account_id"],
                "balance": account["balance"],
                "equity": account["equity"],
                "currency": account["currency"],
            },
            idempotent_replay=idempotent_replay,
        )

    # -- idempotent replay -----------------------------------------------

    def _replay(
        self,
        position_id: str,
        decision_id: str,
        *,
        provider_name: str | None,
    ) -> ManagementResult | None:
        record = self._store.get_decision(decision_id)
        if record is None:
            return None
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise ManagementRejected(
                "DATA_UNAVAILABLE", "the stored management decision is unreadable"
            )
        recorded_provider = (payload.get("provenance") or {}).get("provider")
        if (
            provider_name
            and isinstance(recorded_provider, str)
            and provider_name.strip().lower() != recorded_provider.strip().lower()
        ):
            raise ManagementRejected(
                "IDEMPOTENCY_CONFLICT",
                "the same Idempotency-Key was used with a different provider",
            )
        if record.validation_status == "REJECTED":
            errors = payload.get("validation_errors") or []
            first = errors[0] if errors else {}
            raise ManagementRejected(
                _llm_error_code(str(first.get("code") or "")),
                str(payload.get("rejected_reason") or "the management decision was rejected"),
                field=first.get("field"),
            )
        management = payload.get("position_management") or {}
        action = str(management.get("action") or "HOLD")
        position_record = self._store.get_position(position_id)
        if position_record is None:
            raise ManagementRejected("POSITION_NOT_FOUND", f"position {position_id} does not exist")
        position_payload = json.loads(position_record.payload_json)
        history = position_payload.get("management_history") or []
        entry = next(
            (
                item
                for item in reversed(history)
                if isinstance(item, dict) and item.get("decision_id") == decision_id
            ),
            None,
        )
        llm_view = {
            "decision_id": decision_id,
            "action": action,
            "validation_status": record.validation_status,
            "confidence": payload.get("confidence"),
            "rationale": payload.get("rationale"),
            "provenance": payload.get("provenance"),
        }
        if entry is None:
            return ManagementResult(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                status="BLOCKED",
                executed=False,
                reason_code="RISK_BLOCKED",
                reason="the original management action was blocked; no order was created",
                llm=llm_view,
                position=position_payload,
                account=self._account_view(),
                idempotent_replay=True,
            )
        order_id = entry.get("executed_order_id")
        if order_id is None:
            return ManagementResult(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                status="NO_ACTION",
                executed=False,
                reason_code="HOLD",
                reason="the original management decision was HOLD; no order was created",
                llm=llm_view,
                position=position_payload,
                account=self._account_view(),
                idempotent_replay=True,
            )
        order = self._store.get_order(str(order_id))
        if order is None:
            raise ManagementRejected(
                "DATA_UNAVAILABLE", f"the replayed order {order_id} is missing"
            )
        if position_payload.get("state") == "OPEN":
            # A stop/target update is a second transaction after the fill. If it
            # was lost (crash between the two commits), a same-key replay must
            # apply it idempotently instead of reporting success with the stale
            # stop still in place.
            position_payload = self._reconcile_replayed_change(
                position_id=position_id,
                decision_id=decision_id,
                decision=payload,
                position_payload=position_payload,
            )
        order_payload = json.loads(order.payload_json)
        fill = self._store.get_fill_by_order_id(str(order_id))
        trade = self._store.get_trade_log_for_position(position_id)
        return ManagementResult(
            position_id=position_id,
            decision_id=decision_id,
            action=action,
            status="EXECUTED",
            executed=True,
            reason_code=action,
            reason="the original management result was replayed",
            llm=llm_view,
            risk=self._risk_view(str(order_payload.get("risk_verdict_id") or "")),
            order=order_payload,
            fill=None if fill is None else json.loads(fill.payload_json),
            position=position_payload,
            trade_log=None if trade is None else json.loads(trade.payload_json),
            account=self._account_view(),
            idempotent_replay=True,
        )

    def _reconcile_replayed_change(
        self,
        *,
        position_id: str,
        decision_id: str,
        decision: dict[str, Any],
        position_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently apply a stored stop/target change missing from the row.

        A stop/target update is a second transaction after the fill. If it was
        lost (a crash between the two commits), a same-key replay must apply it
        exactly once. The write is guarded so a stale key can never roll back a
        newer change: the matched decision must be the last management action,
        the row must still carry the pre-change stop/targets recorded at decision
        time, and the recovered stop is validated against the decision-time mark.
        When any of these cannot be established the replay returns the stored
        result untouched (fail closed).
        """
        management = decision.get("position_management") or {}
        action = str(management.get("action") or "")
        if action not in {
            ManagementAction.PARTIAL_CLOSE.value,
            ManagementAction.ADD.value,
        }:
            return position_payload
        new_stop = json_decimal(management.get("new_stop_price"))
        raw_targets = management.get("new_targets") or []
        targets = (
            tuple(Decimal(str(target)) for target in raw_targets)
            if isinstance(raw_targets, list) and raw_targets
            else None
        )
        if new_stop is None and targets is None:
            return position_payload
        history = position_payload.get("management_history")
        if not isinstance(history, list) or not history:
            return position_payload
        last = history[-1]
        if not isinstance(last, dict) or last.get("decision_id") != decision_id:
            # A later management action exists; its stop/targets win.
            return position_payload
        snapshot = self._management_snapshot(decision_id)
        if snapshot is None:
            # No pre-change snapshot: a crash gap cannot be distinguished from a
            # stale key replaying over a newer change, so do not rewrite.
            return position_payload
        persisted_stop = json_decimal(position_payload.get("stop_price"))
        recorded_stop = json_decimal(snapshot.get("stop_price"))
        if persisted_stop != recorded_stop:
            # The row no longer carries the values this decision changed from.
            return position_payload
        persisted_targets = [
            json_decimal(target) for target in (position_payload.get("target_prices") or [])
        ]
        recorded_targets = [
            json_decimal(target) for target in (snapshot.get("target_prices") or [])
        ]
        if persisted_targets != recorded_targets:
            return position_payload
        stop_applied = new_stop is None or persisted_stop == new_stop
        targets_applied = targets is None or list(targets) == persisted_targets
        if stop_applied and targets_applied:
            return position_payload
        decision_mark = json_decimal(snapshot.get("mark_price"))
        if new_stop is not None and decision_mark is None:
            return position_payload
        try:
            classification = classify_management_intent(
                action=action,
                position_side=str(position_payload.get("side") or ""),
                current_stop=recorded_stop,
                new_stop_price=new_stop,
            )
        except ValueError:
            return position_payload
        now = utc_timestamp(self._clock.now())
        record_audit(
            self._store,
            event_type="MANAGEMENT_RECONCILED",
            actor={"kind": "SYSTEM", "id": "management_service"},
            subject={"kind": "POSITION", "id": position_id},
            payload={
                "decision_id": decision_id,
                "action": action,
                "risk_increasing": classification.risk_increasing,
                "stop_change": classification.stop_change.value,
                "reasons": list(classification.reasons),
            },
            at=now,
        )
        if classification.risk_increasing:
            try:
                self._kill_switch.require_clear()
            except KillSwitchActive as exc:
                raise ManagementRejected(exc.code, exc.message) from exc
        return self._positions.apply_management_stop(
            position_id,
            stop_price=new_stop,
            target_prices=targets,
            decision_id=decision_id,
            now=now,
            reference=decision_mark,
        )

    def _management_snapshot(self, decision_id: str) -> dict[str, Any] | None:
        """The pre-change stop/targets/mark recorded for this decision, if any.

        It is written with the kill-switch classification audit before execution.
        If the audit is missing (a crash before it committed) the caller must not
        reconcile, because the pre-change values cannot be checked.
        """
        for event in reversed(self._store.list_audit_events_ascending(limit=None)):
            if event.event_type != "MANAGEMENT_CLASSIFIED":
                continue
            try:
                body = json.loads(event.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            payload = body.get("payload") if isinstance(body, dict) else None
            if not isinstance(payload, dict) or payload.get("decision_id") != decision_id:
                continue
            snapshot = payload.get("position_before")
            if isinstance(snapshot, dict):
                return snapshot
        return None

    def _account_view(self) -> dict[str, Any]:
        summary = self._paper.summary()
        return {
            "account_id": summary["account_id"],
            "balance": summary["balance"],
            "equity": summary["equity"],
            "currency": summary["currency"],
        }

    def _provider_name(self, requested: str | None) -> str:
        value = (requested or "").strip().lower()
        if self._provider is None:
            raise ManagementRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                "No LLM provider is configured; the management decision cannot run.",
            )
        name = value or str(getattr(self._provider, "provider", "") or "")
        if not name:
            capabilities = getattr(self._provider, "capabilities", None)
            if callable(capabilities):
                name = str(getattr(capabilities(), "provider", "") or "")
        if requested and name != requested.strip().lower():
            # ``provider_for`` already resolved the request; a mismatch means the
            # caller named a different provider than the injected adapter.
            raise ManagementRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"No {requested} provider is configured for the management role.",
            )
        if not provider_configured(self._settings, name):
            raise ManagementRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"No {name or 'LLM'} API key is configured; the management decision cannot run.",
            )
        return name


def _decision_id(position_id: str, idempotency_key: str) -> str:
    """The manual management decision id, derived from the Idempotency-Key.

    There are two ``MGD`` id schemes, both single-sourced per trigger: the
    manual ``oft pos manage`` path uses this key-derived id (the same key must
    replay the same decision), while the monitor path uses the mark-derived
    :func:`oft.domain.positions.management_decision_id` for its recorded-mark
    decision. A caller never supplies either id.
    """
    digest = hashlib.sha256(f"{position_id}:{idempotency_key}".encode()).hexdigest()[:16]
    return f"MGD-{digest.upper()}"


def _order_key(decision_id: str, purpose: str) -> str:
    return f"{decision_id}:{purpose}"


def _approval_id(decision_id: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{decision_id}:{purpose}".encode()).hexdigest()[:16]
    return f"APR-{digest.upper()}"


def _note(management: dict[str, Any]) -> str:
    rationale = str(management.get("rationale") or "").strip()
    urgency = str(management.get("urgency") or "")
    base = f"management {management.get('action', '').lower()}"
    if urgency:
        base += f" ({urgency.lower()} urgency)"
    if rationale:
        base += f": {rationale[:200]}"
    return base


__all__ = ["ManagementRejected", "ManagementResult", "ManagementService"]
