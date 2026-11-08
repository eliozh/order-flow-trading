"""Entry re-check, deterministic risk verdict, and the human paper accept/reject.

The LLM only proposes an entry. The risk gate recomputes every cap it has an
input for from current account state; the daily-loss cap is not evaluated here
because this slice has no realized-PnL source, and the verdict records that
input as unevaluated (slice-18 follow-up). The execution gateway submits the
reduced/approved intent; the paper broker fills it deterministically. No path
bypasses the gateway.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from oft.application.budget_ledger import BudgetEntry, BudgetLedger, stable_call_id
from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch, KillSwitchActive, record_audit
from oft.application.knowledge_service import KnowledgeService
from oft.application.llm_validation import (
    ValidationIssue,
    ValidationOutcome,
    decision_envelope,
    hash_text,
    schema_text,
    validate_output,
)
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.prompt_builder import KnowledgeInjection, absent_injection
from oft.application.prompt_service import (
    PromptService,
    RenderedPrompt,
    secret_values_from_settings,
)
from oft.application.trigger_service import TriggerNotFound
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.prompts import PromptRole, rendered_prompt_hash
from oft.domain.risk import (
    DEFAULT_CAPS,
    RiskRequest,
    evaluate_risk,
    verdict_to_contract,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import LLMPort, LLMRequest, LLMResult, LLMRole, LLMUsage
from oft.ports.store import (
    DecisionRecord,
    LLMCallRecord,
    RiskVerdictRecord,
    StorePort,
    TriggerEventRecord,
)

ENTRY_PROMPT_ID = "entry_recheck"
ENTRY_PROMPT_VERSION = 1


class EntryRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EntryResult:
    decision_id: str
    risk_verdict: dict[str, Any]
    order: dict[str, Any] | None
    fill: dict[str, Any] | None
    position: dict[str, Any] | None
    account: dict[str, Any] | None
    status: str
    idempotent_replay: bool = False


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()


class EntryService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: LLMPort | None,
        settings: Any,
        kill_switch: KillSwitch,
    ) -> None:
        self._store = store
        self._clock = clock
        self._provider = provider
        self._settings = settings
        self._kill_switch = kill_switch
        self._budget = BudgetLedger(store=store)
        self._broker = PaperBroker(settings=settings, store=store, clock=clock)
        self._secrets = secret_values_from_settings(settings) if settings is not None else ()
        self._prompts = PromptService(store=store, clock=clock, secret_values=self._secrets)
        self._knowledge = (
            KnowledgeService(settings=settings, clock=clock) if settings is not None else None
        )
        self._gateway = ExecutionGateway(
            store=store,
            broker=self._broker,
            kill_switch=kill_switch,
            clock=clock,
        )

    # -- re-check --------------------------------------------------------

    def recheck(self, trigger_id: str) -> dict[str, Any]:
        if self._provider is None:
            raise EntryRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                "No LLM provider is configured; the entry re-check cannot run.",
            )
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        opportunity = self._opportunity(payload["opportunity_id"])
        timeframe = self._timeframe(opportunity)
        now = utc_timestamp(self._clock.now())
        knowledge = self._knowledge_injection()
        rendered = self._prompt(payload, opportunity, timeframe, knowledge.text)
        system, user = rendered.system, rendered.user
        digest = hash_text(f"{system}\n{user}\n{schema_text()}")
        decision_id = f"DEC-ENTRY-{digest[:16].upper()}"
        request = LLMRequest(
            role=LLMRole.ENTRY_RECHECK,
            model="fixture",
            system_prompt=system,
            user_prompt=user,
            schema_json=schema_text(),
            input_hash=digest,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_hash=rendered.prompt_hash,
            request_id=f"REQ-{digest[:12].upper()}",
        )
        calls: list[LLMCallRecord] = []
        budgets: list[BudgetEntry] = []
        provider = self._provider

        def call_provider(attempt: int, prompt: str) -> LLMResult:
            result: LLMResult = provider.complete(replace(request, user_prompt=prompt))
            call_id = stable_call_id(decision_id, attempt)
            call = LLMCallRecord(
                call_id=call_id,
                decision_id=decision_id,
                role=LLMRole.ENTRY_RECHECK.value,
                provider=result.provider,
                model=result.model,
                prompt_id=rendered.prompt_id,
                prompt_version=rendered.version,
                prompt_hash=rendered_prompt_hash(
                    rendered.prompt_id, rendered.version, system, prompt
                ),
                input_hash=digest,
                attempt=attempt,
                request_id=result.request_id,
                status="OK",
                raw_output_hash=hash_text(result.raw_text),
                usage_json=json.dumps(result.usage.to_contract(), sort_keys=True),
                latency_ms=result.latency_ms,
                error_code=None,
                created_at=now,
                system_prompt_id=rendered.system_prompt_id,
                system_prompt_version=rendered.system_version,
                system_prompt_hash=rendered.system_prompt_hash,
            )
            self._store.save_llm_call(call)
            calls.append(call)
            budgets.append(
                self._budget.record_call(
                    call_id=call_id,
                    decision_id=decision_id,
                    role=LLMRole.ENTRY_RECHECK.value,
                    result=result,
                    occurred_at=now,
                )
            )
            return result

        first = call_provider(1, user)
        if first.refusal:
            outcome = ValidationOutcome(
                status="REJECTED",
                output=None,
                errors=(ValidationIssue("output", "LLM_REFUSED", "the model refused"),),
                raw_output_hash=hash_text(first.raw_text),
            )
        elif first.truncated:
            outcome = ValidationOutcome(
                status="REJECTED",
                output=None,
                errors=(ValidationIssue("output", "LLM_OUTPUT_TRUNCATED", "output truncated"),),
                raw_output_hash=hash_text(first.raw_text),
            )
        else:
            outcome = validate_output(
                first.raw_text,
                role=LLMRole.ENTRY_RECHECK.value,
                repair=lambda message: (
                    call_provider(
                        2,
                        f"{user}\n\nYour previous output failed validation: {message}. "
                        "Return only a corrected JSON object matching the schema.",
                    ).raw_text
                ),
            )

        subject = {
            "symbol": payload["symbol"],
            "timeframe": timeframe,
            "as_of": now,
            "scan_id": opportunity.source.get("scan_id"),
            "report_id": opportunity.source.get("report_id"),
            "opportunity_id": payload["opportunity_id"],
            "trigger_id": trigger_id,
            "position_id": None,
            "environment": "PAPER",
        }
        envelope = decision_envelope(
            decision_id=decision_id,
            role=LLMRole.ENTRY_RECHECK.value,
            output=outcome.output,
            subject=subject,
            provenance=self._provenance(
                first,
                digest,
                rendered,
                calls,
                budgets,
                now,
                knowledge.artifact_id,
            ),
            validation_status=outcome.status,
            validation_errors=outcome.errors,
            rejected_reason=(
                None if outcome.valid else f"{outcome.errors[0].code}: {outcome.errors[0].message}"
            ),
            raw_output_hash=outcome.raw_output_hash,
            persisted_at=now,
        )
        self._store.save_decision(
            DecisionRecord(
                decision_id=decision_id,
                role=LLMRole.ENTRY_RECHECK.value,
                decision_type="ENTRY_RECHECK",
                validation_status=outcome.status,
                raw_output_hash=outcome.raw_output_hash,
                persisted_at=now,
                payload_json=json.dumps(envelope, sort_keys=True),
            )
        )
        no_entry = (
            outcome.valid
            and outcome.output is not None
            and outcome.output.entry_recheck is not None
            and outcome.output.entry_recheck.decision == "NO_ENTRY"
        )
        next_status = "REJECTED" if (not outcome.valid or no_entry) else "RECHECKED"
        updated = dict(payload)
        updated["entry_decision_id"] = decision_id
        updated["status"] = next_status
        if not outcome.valid:
            updated["rejection_reason"] = envelope["rejected_reason"]
        record = replace(
            trigger,
            status=next_status,
            payload_json=json.dumps(updated, sort_keys=True),
        )
        self._store.update_trigger_event(record)
        record_audit(
            self._store,
            event_type="ENTRY_RECHECK",
            actor={"kind": "LLM", "id": decision_id},
            subject={"kind": "TRIGGER", "id": trigger_id},
            payload={
                "validation_status": outcome.status,
                "decision_id": decision_id,
                "status": next_status,
            },
            at=now,
        )
        return updated

    # -- human actions ---------------------------------------------------

    def accept(
        self,
        trigger_id: str,
        *,
        idempotency_key: str,
        actor_id: str = "cli",
    ) -> EntryResult:
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        if payload["status"] == "ACCEPTED" and payload.get("created_order_id"):
            return self._replay(payload["created_order_id"], payload)
        existing_order = self._store.get_order_by_idempotency(idempotency_key)
        if existing_order is not None:
            existing_payload = json.loads(existing_order.payload_json)
            risk_verdict_id = str(existing_payload.get("risk_verdict_id") or "")
            verdict_record = (
                self._store.get_risk_verdict(risk_verdict_id) if risk_verdict_id else None
            )
            if verdict_record is None or verdict_record.trigger_id != trigger_id:
                raise EntryRejected(
                    "CONFLICT",
                    "the idempotency key belongs to a different trigger",
                )
            return self._replay(existing_order.order_id, payload)
        if payload["status"] in {"REJECTED", "BLOCKED", "EXPIRED"}:
            raise EntryRejected("CONFLICT", f"trigger is {payload['status']}")

        try:
            self._kill_switch.require_clear()
        except KillSwitchActive as exc:
            raise EntryRejected(exc.code, exc.message) from exc
        if payload.get("entry_decision_id") is None:
            payload = self.recheck(trigger_id)
            trigger = self._trigger(trigger_id)
        decision = self._store.get_decision(str(payload["entry_decision_id"]))
        if decision is None or decision.validation_status not in {"VALID", "REPAIRED"}:
            reason = "entry decision is rejected or missing"
            self._update_trigger(trigger, status="REJECTED", rejection_reason=reason)
            raise EntryRejected("ORDER_REJECTED", reason)
        decision_payload = json.loads(decision.payload_json)
        recheck = decision_payload.get("entry_recheck") or {}
        if recheck.get("decision") == "NO_ENTRY":
            self._update_trigger(
                trigger, status="REJECTED", rejection_reason="entry re-check returned NO_ENTRY"
            )
            self._invalidate_opportunity(payload["opportunity_id"], "NO_ENTRY")
            raise EntryRejected("ORDER_REJECTED", "entry re-check returned NO_ENTRY")
        intent = recheck.get("order_intent")
        if intent is None:
            self._update_trigger(
                trigger, status="REJECTED", rejection_reason="entry intent is missing"
            )
            raise EntryRejected("ORDER_REJECTED", "entry intent is missing")

        opportunity = self._opportunity(payload["opportunity_id"])
        reference_price, mark_as_of = self._mark(opportunity)
        side = PositionSide.LONG if payload["side"] == "LONG" else PositionSide.SHORT
        # The broker fills at the reference mark adjusted by the configured
        # slippage, so the risk caps are always sized from the mark. The
        # LLM-supplied entry_price is advisory only and never feeds the risk math.
        risk_entry_price = reference_price
        contract = self._store.get_symbol(payload["symbol"])
        if contract is None:
            # Fail closed: without tick/step/min-notional the gate would size and
            # round as if every filter were zero, which is unsafe.
            raise EntryRejected(
                "DATA_UNAVAILABLE",
                f"the symbol contract for {payload['symbol']} is missing",
            )
        now = utc_timestamp(self._clock.now())
        account = self._broker.account()
        position_count = len(self._store.list_positions(state="OPEN"))
        try:
            # size_mode is exclusive: only the governing sizing field is read,
            # and a persisted intent that violates the mode is rejected rather
            # than silently sized by the other field. Parsing lives inside this
            # guard so malformed persisted numerics become EntryRejected.
            size_mode = intent.get("size_mode")
            if size_mode == "RISK_PCT":
                if intent.get("risk_pct") is None:
                    raise EntryRejected("ORDER_REJECTED", "size_mode RISK_PCT requires risk_pct")
                risk_pct = float(intent["risk_pct"])
                notional = None
            elif size_mode == "QUOTE_NOTIONAL":
                if intent.get("quote_notional_usd") is None:
                    raise EntryRejected(
                        "ORDER_REJECTED",
                        "size_mode QUOTE_NOTIONAL requires quote_notional_usd",
                    )
                risk_pct = DEFAULT_CAPS.per_trade_risk_pct
                notional = Decimal(str(intent["quote_notional_usd"]))
            else:
                raise EntryRejected("ORDER_REJECTED", f"unknown size_mode: {size_mode!r}")
            stop_price = Decimal(str(intent["stop_price"])) if intent.get("stop_price") else None
            leverage = float(intent["leverage"]) if intent.get("leverage") is not None else 1.0
            take_profits = tuple(Decimal(str(value)) for value in intent.get("take_profits", []))
            verdict = evaluate_risk(
                request=RiskRequest(
                    symbol=payload["symbol"],
                    side=side.value,
                    entry_price=risk_entry_price,
                    stop_price=stop_price,
                    quantity=None,
                    risk_pct=risk_pct,
                    leverage=leverage,
                    notional_usd=notional,
                ),
                caps=DEFAULT_CAPS,
                equity=Decimal(account.balance),
                open_positions=position_count,
                # No realized-PnL source exists in this slice, so the daily-loss
                # cap is passed as unavailable and recorded as unevaluated
                # instead of a silent zero (slice-18 follow-up).
                daily_loss_usd=None,
                mark_price=reference_price,
                mark_as_of=mark_as_of,
                now=now,
                tick_size=contract.tick_size,
                step_size=contract.step_size,
                min_notional=contract.min_notional,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # A malformed persisted intent must not surface as an unhandled 500.
            raise EntryRejected("ORDER_REJECTED", f"the entry intent is invalid: {exc}") from exc
        risk_verdict_id = f"RISK-{_digest(f'{trigger_id}:{decision.decision_id}')}"
        requested_payload = {
            "entry_price": str(risk_entry_price),
            "stop_price": str(stop_price) if stop_price is not None else "0",
            "quantity": None,
            "risk_pct": risk_pct,
            "leverage": leverage,
            "notional_usd": str(notional) if notional is not None else None,
        }
        verdict_payload = verdict_to_contract(verdict, requested_payload=requested_payload)
        self._store.save_risk_verdict(
            RiskVerdictRecord(
                risk_verdict_id=risk_verdict_id,
                trigger_id=trigger_id,
                decision_id=decision.decision_id,
                verdict=verdict.verdict.value,
                binding_cap=verdict.binding_cap.value,
                computed_at=verdict.computed_at,
                expires_at=verdict.expires_at,
                payload_json=json.dumps(verdict_payload, sort_keys=True),
            )
        )
        record_audit(
            self._store,
            event_type="RISK_VERDICT",
            actor={"kind": "SYSTEM", "id": "risk_gate"},
            subject={"kind": "RISK_VERDICT", "id": risk_verdict_id},
            payload={"verdict": verdict.verdict, "binding_cap": verdict.binding_cap},
            at=now,
        )
        if verdict.verdict == "BLOCK" or verdict.allowed_quantity is None:
            self._update_trigger(
                trigger,
                status="BLOCKED",
                rejection_reason=f"risk blocked: {verdict.binding_cap}",
                risk_verdict=verdict_payload,
            )
            raise EntryRejected(
                "RISK_BLOCKED",
                f"risk gate blocked the entry ({verdict.binding_cap})",
            )
        approval_id = f"APR-{_digest(f'{trigger_id}:{idempotency_key}')}"
        order_intent = OrderIntent(
            symbol=payload["symbol"],
            side=OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=verdict.allowed_quantity,
            price=None,
            stop_price=stop_price,
            take_profits=take_profits,
            leverage=verdict.allowed_leverage,
            decision_id=decision.decision_id,
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            position_side=side,
            risk_amount_usd=verdict.risk_amount_usd,
            risk_pct=risk_pct,
        )
        try:
            result = self._gateway.submit(
                order_intent,
                AuthContext(actor_id=actor_id, approval_id=approval_id),
                mark_price=reference_price,
                mark_as_of=mark_as_of,
            )
        except GatewayError as exc:
            raise EntryRejected(exc.code, exc.message) from exc

        human_action = {
            "action": "ACCEPT",
            "actor": {"kind": "USER", "id": actor_id},
            "at": now,
            "reason": None,
            "confirmation_token_used": False,
        }
        self._update_trigger(
            trigger,
            status="ACCEPTED",
            risk_verdict=verdict_payload,
            human_action=human_action,
            created_order_id=result.order.order_id,
            entry_decision_id=decision.decision_id,
        )
        record_audit(
            self._store,
            event_type="TRIGGER_ACCEPTED",
            actor={"kind": "USER", "id": actor_id},
            subject={"kind": "TRIGGER", "id": trigger_id},
            payload={"order_id": result.order.order_id, "environment": "PAPER"},
            at=now,
        )
        return EntryResult(
            decision_id=decision.decision_id,
            risk_verdict=verdict_payload,
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=result.position.to_contract(),
            account={
                "account_id": result.account.account_id,
                "balance": result.account.balance,
                "currency": result.account.currency,
            },
            status="ACCEPTED",
        )

    def reject(self, trigger_id: str, *, reason: str, actor_id: str = "cli") -> dict[str, Any]:
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        if payload["status"] == "ACCEPTED":
            raise EntryRejected("CONFLICT", "an accepted trigger cannot be rejected")
        now = utc_timestamp(self._clock.now())
        payload["status"] = "REJECTED"
        payload["rejection_reason"] = reason
        payload["human_action"] = {
            "action": "REJECT",
            "actor": {"kind": "USER", "id": actor_id},
            "at": now,
            "reason": reason,
            "confirmation_token_used": False,
        }
        record = replace(
            trigger,
            status="REJECTED",
            payload_json=json.dumps(payload, sort_keys=True),
        )
        self._store.update_trigger_event(record)
        self._invalidate_opportunity(payload["opportunity_id"], reason)
        record_audit(
            self._store,
            event_type="TRIGGER_REJECTED",
            actor={"kind": "USER", "id": actor_id},
            subject={"kind": "TRIGGER", "id": trigger_id},
            payload={"reason": reason},
            at=now,
        )
        return payload

    # -- helpers ---------------------------------------------------------

    def _trigger(self, trigger_id: str) -> TriggerEventRecord:
        record = self._store.get_trigger_event(trigger_id)
        if record is None:
            raise TriggerNotFound(trigger_id)
        return record

    def _opportunity(self, opportunity_id: str) -> Any:
        from oft.application.opportunity_service import OpportunityService

        return OpportunityService(store=self._store, clock=self._clock).show(opportunity_id)

    def _mark(self, opportunity: Any) -> tuple[Decimal, str]:
        report_id = str(opportunity.source.get("report_id", ""))
        report = self._store.get_report(report_id) if report_id else None
        if report is None:
            raise EntryRejected("DATA_UNAVAILABLE", "the source report snapshot is missing")
        snapshot = json.loads(report.snapshot_json)
        value = snapshot.get("last_price") or snapshot.get("mark_price")
        if value is None:
            raise EntryRejected("DATA_UNAVAILABLE", "the snapshot has no reference price")
        as_of = snapshot.get("as_of")
        if not isinstance(as_of, str) or not as_of:
            raise EntryRejected("DATA_UNAVAILABLE", "the snapshot has no as_of timestamp")
        return Decimal(str(value)), as_of

    def _timeframe(self, opportunity: Any) -> str:
        """The scan period this opportunity came from (never a hardcoded literal)."""
        scan_id = str(opportunity.source.get("scan_id") or "")
        scan = self._store.get_scan(scan_id) if scan_id else None
        if scan is not None and scan.period:
            return scan.period
        report_id = str(opportunity.source.get("report_id") or "")
        report = self._store.get_report(report_id) if report_id else None
        if report is not None and report.timeframe:
            return report.timeframe
        raise EntryRejected(
            "DATA_UNAVAILABLE",
            "the opportunity has no source scan period for the entry re-check",
        )

    def _knowledge_injection(self) -> KnowledgeInjection:
        if self._knowledge is None:
            return absent_injection(PromptRole.ENTRY_RECHECK.value)
        return self._knowledge.injection_for(PromptRole.ENTRY_RECHECK.value)

    def _prompt(
        self,
        payload: dict[str, Any],
        opportunity: Any,
        timeframe: str,
        knowledge: str,
    ) -> RenderedPrompt:
        return self._prompts.render_active(
            PromptRole.ENTRY_RECHECK,
            {
                "symbol": payload["symbol"],
                "timeframe": timeframe,
                "side": payload["side"],
                "trigger": json.dumps(payload["condition_results"], sort_keys=True),
                "thesis": opportunity.thesis,
                "knowledge": knowledge,
                "schema": schema_text(),
            },
            secret_values=self._secrets,
        )

    def _provenance(
        self,
        result: LLMResult,
        digest: str,
        rendered: RenderedPrompt,
        calls: list[LLMCallRecord],
        budgets: list[BudgetEntry],
        now: str,
        knowledge_artifact_hash: str | None,
    ) -> dict[str, Any]:
        cost = budgets[0].payload if budgets else None
        return {
            "provider": result.provider,
            "model": result.model,
            "model_snapshot": None,
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.version,
            "prompt_hash": rendered.prompt_hash,
            "knowledge_artifact_hash": knowledge_artifact_hash,
            "input_hash": digest,
            "schema_version": "1.0.0",
            "temperature": 0.0,
            "seed": None,
            "request_id": result.request_id,
            "latency_ms": result.latency_ms,
            "usage": json.loads(calls[0].usage_json) if calls else LLMUsage().to_contract(),
            "cost": {
                "amount_usd": cost["cost_usd"] if cost else "0",
                "currency": "USD",
                "price_version": cost["price_version"] if cost else "stub/v1",
                "unpriced": bool(cost["unpriced"]) if cost else True,
            },
            "recorded_at": now,
        }

    def _update_trigger(
        self,
        trigger: TriggerEventRecord,
        *,
        status: str,
        rejection_reason: str | None = None,
        risk_verdict: dict[str, Any] | None = None,
        human_action: dict[str, Any] | None = None,
        created_order_id: str | None = None,
        entry_decision_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        payload["status"] = status
        payload["rejection_reason"] = rejection_reason
        if risk_verdict is not None:
            payload["risk_verdict"] = risk_verdict
        if human_action is not None:
            payload["human_action"] = human_action
        if created_order_id is not None:
            payload["created_order_id"] = created_order_id
        if entry_decision_id is not None:
            payload["entry_decision_id"] = entry_decision_id
        record = replace(trigger, status=status, payload_json=json.dumps(payload, sort_keys=True))
        self._store.update_trigger_event(record)
        return payload

    def _invalidate_opportunity(self, opportunity_id: str, reason: str) -> None:
        from oft.application.opportunity_service import OpportunityService
        from oft.domain.opportunities import OpportunityTransitionError

        service = OpportunityService(store=self._store, clock=self._clock)
        try:
            service.invalidate(opportunity_id, reason=reason)
        except OpportunityTransitionError:
            # A repeat reject may find the opportunity already terminal; that is
            # the only transition failure we tolerate. Anything else propagates.
            return

    def _replay(self, order_id: str, trigger_payload: dict[str, Any]) -> EntryResult:
        order = self._store.get_order(order_id)
        if order is None:
            raise EntryRejected("ORDER_NOT_FOUND", f"Order {order_id} does not exist.")
        order_payload = json.loads(order.payload_json)
        position_id = order_payload.get("position_id")
        fill = self._store.get_fill_by_order_id(order_id)
        position = self._store.get_position(position_id) if position_id else None
        account = self._broker.account()
        return EntryResult(
            decision_id=str(trigger_payload.get("entry_decision_id") or ""),
            risk_verdict=trigger_payload.get("risk_verdict") or {},
            order=order_payload,
            fill=json.loads(fill.payload_json) if fill is not None else None,
            position=json.loads(position.payload_json) if position else None,
            account={
                "account_id": account.account_id,
                "balance": account.balance,
                "currency": account.currency,
            },
            status="ACCEPTED",
            idempotent_replay=True,
        )


__all__ = [
    "ENTRY_PROMPT_ID",
    "ENTRY_PROMPT_VERSION",
    "EntryRejected",
    "EntryResult",
    "EntryService",
]
