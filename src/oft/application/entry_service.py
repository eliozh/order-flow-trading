"""Entry re-check, deterministic risk verdict, and the human paper accept/reject.

The LLM only proposes an entry. The risk gate recomputes every percentage-based
cap from current account equity and the trigger snapshot, persisting the verdict
and an ordered decision card (TRIGGER -> LLM -> RISK -> HUMAN). There is no
realized-PnL source yet, so the daily-loss input is recorded as stale rather
than treated as zero. The execution gateway submits the reduced/approved intent;
the paper broker fills it deterministically. No path bypasses the gateway.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from oft.application.budget_ledger import BudgetEntry, BudgetLedger
from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch, KillSwitchActive, record_audit
from oft.application.knowledge_service import KnowledgeService, knowledge_injection
from oft.application.llm_orchestrator import (
    LLMConfigurationError,
    LLMOrchestrator,
    LLMRuntimeRegistry,
    provider_configured,
)
from oft.application.llm_validation import (
    decision_envelope,
    hash_text,
    schema_text,
)
from oft.application.paper_account import PaperAccountService, compute_equity
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.prompt_builder import KnowledgeInjection
from oft.application.prompt_service import (
    PromptService,
    RenderedPrompt,
    secret_values_from_settings,
)
from oft.application.trigger_service import TriggerNotFound
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.pricing import PriceTableError
from oft.domain.prompts import PromptRole
from oft.domain.risk import (
    DEFAULT_CAPS,
    RISK_CAPS_STATE_KEY,
    RiskCaps,
    RiskRequest,
    RiskVerdict,
    apply_persisted_caps,
    evaluate_risk,
    validate_caps,
    verdict_to_contract,
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
    OrderRecord,
    RiskVerdictRecord,
    StorePort,
    TriggerEventRecord,
)

ENTRY_PROMPT_ID = "entry_recheck"
ENTRY_PROMPT_VERSION = 1

# The only states in which a human may still act on the trigger. Everything
# else (ACCEPTED/REJECTED/BLOCKED/EXPIRED) is terminal for this slice. The
# accept/reject/recheck paths also consult the OMS ledger, so a crashed accept
# (order committed, trigger update lost) is reconciled instead of overwritten.
_PRE_ACCEPT_STATUSES = frozenset({"PENDING_RECHECK", "RECHECKED"})

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _total_usage(calls: list[LLMCallRecord]) -> dict[str, int]:
    """Sum usage across every call row of a decision (including repairs)."""
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
    """Sum cost across every budget row of a decision (including repairs)."""
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


def _decimal_or_none(value: object) -> Decimal | None:
    """Parse a verdict value; ``None`` and the contract's "0" mean absent."""
    if value is None or value == "0":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


class RiskCapsError(Exception):
    """A persisted risk-caps document is unreadable or invalid (fail closed)."""

    code = "VALIDATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def caps_from_settings(settings: Settings | None) -> RiskCaps:
    """The configured caps, or the documented defaults when settings are absent."""
    if settings is None:
        return DEFAULT_CAPS
    return RiskCaps(
        per_trade_risk_pct=float(settings.risk_per_trade_pct),
        max_concurrent_positions=int(settings.risk_max_concurrent_positions),
        daily_loss_pct=float(settings.risk_daily_loss_pct),
        max_leverage=float(settings.risk_max_leverage),
        max_notional_pct=float(settings.risk_max_notional_pct),
        max_slippage_bps=float(settings.risk_max_slippage_bps),
        max_spread_bps=float(settings.risk_max_spread_bps),
        stale_mark_seconds=int(settings.risk_stale_mark_seconds),
    )


def effective_caps(settings: Settings | None, store: StorePort) -> tuple[RiskCaps, str]:
    """Return ``(caps, source)``; persisted fields override the config base (D1).

    The merge is field by field: a partial operator row may tighten one cap and
    must not silently reset the others to the hardcoded dataclass defaults.
    """
    base = caps_from_settings(settings)
    raw = store.get_system_state(RISK_CAPS_STATE_KEY)
    if raw is None:
        return base, "config"
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("persisted risk caps must be a JSON object")
        merged = apply_persisted_caps(base, payload)
        errors = validate_caps(merged)
        if errors:
            raise ValueError("persisted risk caps are invalid: " + "; ".join(errors))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RiskCapsError(str(exc)) from exc
    return merged, "persisted"


@dataclass(frozen=True)
class _RiskEvaluation:
    verdict: RiskVerdict
    payload: dict[str, Any]
    verdict_id: str
    reference_price: Decimal
    mark_as_of: str


class EntryService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: LLMPort | None,
        settings: Any,
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
        self._broker = PaperBroker(settings=settings, store=store, clock=clock)
        self._paper = (
            PaperAccountService(settings=settings, store=store, clock=clock)
            if settings is not None
            else None
        )
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
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        status = str(payload.get("status"))
        existing_entry = self._store.get_entry_order_for_trigger(trigger_id)
        if (
            existing_entry is not None
            and payload.get("created_order_id") != existing_entry.order_id
        ):
            # Crash window: the OMS commit succeeded but the trigger update was
            # lost. Adopt the committed order; never run another LLM call or
            # rewrite the decision/verdict (F1/S6).
            return self._reconcile_recheck(trigger, existing_entry)
        if payload.get("created_order_id") or status not in _PRE_ACCEPT_STATUSES:
            # Never regress an accepted/terminal trigger or rewrite a decision
            # once an order exists (F1).
            raise EntryRejected(
                "CONFLICT",
                f"trigger {trigger_id} is {status}; a re-check is not allowed",
            )
        if self._provider is None:
            raise EntryRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                "No LLM provider is configured; the entry re-check cannot run.",
            )
        provider_name = str(getattr(self._provider, "provider", "") or "")
        if not provider_name:
            capabilities = getattr(self._provider, "capabilities", None)
            if callable(capabilities):
                provider_name = str(getattr(capabilities(), "provider", "") or "")
        if not provider_configured(self._settings, provider_name):
            # A named provider without a key fails closed without a call (M16).
            raise EntryRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"No {provider_name or 'LLM'} API key is configured; "
                "the entry re-check cannot run.",
            )
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
            # Empty model: the orchestrator resolves ``llm_model_entry_recheck``
            # (with the stub falling back to ``llm_model``).
            model="",
            system_prompt=system,
            user_prompt=user,
            schema_json=schema_text(),
            input_hash=digest,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_hash=rendered.prompt_hash,
            request_id=f"REQ-{digest[:12].upper()}",
            system_prompt_id=rendered.system_prompt_id,
            system_prompt_version=rendered.system_version,
            system_prompt_hash=rendered.system_prompt_hash,
        )
        orchestrator = self._orchestrator
        assert orchestrator is not None
        try:
            orchestration = orchestrator.complete_with_validation(
                request=request,
                decision_id=decision_id,
                role=LLMRole.ENTRY_RECHECK,
            )
        except (LLMConfigurationError, LLMAdapterError, PriceTableError) as exc:
            # Map provider/config/pricing failures to the catalog code instead of
            # letting the route turn them into a 500 INTERNAL_ERROR (H2).
            raise EntryRejected(exc.code, exc.message) from exc
        final = orchestration.final_result
        outcome = orchestration.outcome
        calls = list(orchestration.calls)
        budgets = list(orchestration.budgets)

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
                final,
                digest,
                rendered,
                calls,
                budgets,
                now,
                knowledge.artifact_id,
                knowledge.status,
                knowledge.reason,
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
        entry_payload = envelope.get("entry_recheck") or {}
        no_entry = outcome.valid and entry_payload.get("decision") == "NO_ENTRY"
        next_status = "REJECTED" if (not outcome.valid or no_entry) else "RECHECKED"
        updated = dict(payload)
        updated["entry_decision_id"] = decision_id
        updated["status"] = next_status
        if not outcome.valid:
            updated["rejection_reason"] = envelope["rejected_reason"]

        risk_stage: dict[str, Any] = {
            "verdict": "NOT_EVALUATED",
            "binding_cap": "NONE",
            "reasons": ["no entry intent to evaluate"],
            "provenance": "CALC",
        }
        error_code: str | None = None
        intent = entry_payload.get("order_intent")
        if outcome.valid and not no_entry and intent is not None:
            try:
                evaluation = self._evaluate_and_persist_risk(
                    trigger_id=trigger_id,
                    trigger_payload=updated,
                    decision_id=decision_id,
                    intent=intent,
                    seed=f"{trigger_id}:{decision_id}:recheck",
                )
                updated["risk_verdict"] = evaluation.payload
                risk_stage = {**evaluation.payload, "provenance": "CALC"}
            except RiskCapsError as exc:
                # A corrupt operator override is a validation failure, not a
                # stale-data risk block; surface it as a typed error so the
                # route maps it to 422 instead of a 500 (F5).
                raise EntryRejected(exc.code, exc.message) from exc
            except EntryRejected as exc:
                # A gate-input failure (missing contract/mark, malformed intent)
                # is not a risk BLOCK: the gate never produced a verdict. Mark
                # the trigger terminal, persist no verdict, audit the real error
                # code, and emit no accept guidance (S4).
                error_code = exc.code
                next_status = "REJECTED"
                updated["status"] = next_status
                updated["rejection_reason"] = f"{exc.code}: {exc.message}"
                updated.pop("risk_verdict", None)
                risk_stage = {
                    "verdict": "NOT_EVALUATED",
                    "binding_cap": "NONE",
                    "reasons": [f"{exc.code}: {exc.message}"],
                    "error_code": exc.code,
                    "provenance": "CALC",
                }
        elif no_entry:
            risk_stage["reasons"] = ["entry re-check returned NO_ENTRY"]
        elif not outcome.valid:
            risk_stage["reasons"] = [str(envelope.get("rejected_reason") or "invalid decision")]

        data_quality = self._data_quality(risk_stage, outcome.valid and not no_entry)
        card = self._decision_card(
            trigger_id=trigger_id,
            trigger_payload=updated,
            envelope=envelope,
            risk_stage=risk_stage,
            data_quality=data_quality,
            error_code=error_code,
        )
        record = replace(
            trigger,
            status=next_status,
            payload_json=json.dumps(updated, sort_keys=True),
        )
        self._write_trigger(
            record,
            expected_status=status,
            require_no_order=True,
            require_no_entry_order=True,
        )
        record_audit(
            self._store,
            event_type="ENTRY_RECHECK",
            actor={"kind": "LLM", "id": decision_id},
            subject={"kind": "TRIGGER", "id": trigger_id},
            payload={
                "validation_status": outcome.status,
                "decision_id": decision_id,
                "status": next_status,
                "risk_verdict": risk_stage.get("verdict"),
                "error_code": error_code,
            },
            at=now,
        )
        return card

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
        status = str(payload.get("status"))
        created_order_id = payload.get("created_order_id")
        if created_order_id:
            # A trigger has at most one paper entry. Only the original key may
            # replay the original result; any other key is a conflict and must
            # never create a second order (F1).
            original = self._store.get_order(str(created_order_id))
            if original is not None and original.idempotency_key == idempotency_key:
                return self._replay(str(created_order_id), payload)
            raise EntryRejected(
                "CONFLICT",
                f"an order already exists for trigger {trigger_id}",
            )
        if status not in _PRE_ACCEPT_STATUSES:
            raise EntryRejected("CONFLICT", f"trigger is {status}")
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

        try:
            self._kill_switch.require_clear()
        except KillSwitchActive as exc:
            raise EntryRejected(exc.code, exc.message) from exc
        # Crash-window recovery, before any re-evaluation: an entry order that
        # exists is the only entry this trigger may ever have (D6).
        early_entry = self._store.get_entry_order_for_trigger(trigger_id)
        if early_entry is not None:
            if early_entry.idempotency_key == idempotency_key:
                return self._replay(early_entry.order_id, payload)
            now = utc_timestamp(self._clock.now())
            return self._adopt_entry(trigger, early_entry, now=now, actor_id=actor_id)
        if payload.get("entry_decision_id") is None:
            card = self.recheck(trigger_id)
            trigger = self._trigger(trigger_id)
            payload = json.loads(trigger.payload_json)
            status = str(payload.get("status"))
            # The inline re-check can terminally REJECT the trigger; never fall
            # through to the legacy recompute path from a terminal state (P3).
            if status not in _PRE_ACCEPT_STATUSES:
                reason = str(
                    payload.get("rejection_reason")
                    or card.get("rejection_reason")
                    or "the entry re-check rejected the trigger"
                )
                error_code = card.get("error_code")
                if error_code is not None:
                    raise EntryRejected(str(error_code), reason)
                # NO_ENTRY or an invalid decision follows the same path as the
                # explicit decision checks below: invalidate, then reject.
                self._invalidate_opportunity(payload["opportunity_id"], reason)
                raise EntryRejected("ORDER_REJECTED", reason)
        decision = self._store.get_decision(str(payload["entry_decision_id"]))
        if decision is None or decision.validation_status not in {"VALID", "REPAIRED"}:
            reason = "entry decision is rejected or missing"
            self._update_trigger(
                trigger,
                status="REJECTED",
                rejection_reason=reason,
                expected_status=status,
            )
            raise EntryRejected("ORDER_REJECTED", reason)
        decision_payload = json.loads(decision.payload_json)
        recheck = decision_payload.get("entry_recheck") or {}
        if recheck.get("decision") == "NO_ENTRY":
            self._update_trigger(
                trigger,
                status="REJECTED",
                rejection_reason="entry re-check returned NO_ENTRY",
                expected_status=status,
            )
            self._invalidate_opportunity(payload["opportunity_id"], "NO_ENTRY")
            raise EntryRejected("ORDER_REJECTED", "entry re-check returned NO_ENTRY")
        intent = recheck.get("order_intent")
        if intent is None:
            self._update_trigger(
                trigger,
                status="REJECTED",
                rejection_reason="entry intent is missing",
                expected_status=status,
            )
            raise EntryRejected("ORDER_REJECTED", "entry intent is missing")

        now = utc_timestamp(self._clock.now())
        # Crash-window recovery: the order committed but the trigger update was
        # lost. Reconcile before any re-evaluation (equity has already changed,
        # so a fresh verdict would no longer match). Never a second order (D6).
        existing_entry = self._store.get_entry_order_for_trigger(trigger_id)
        if existing_entry is not None:
            if existing_entry.idempotency_key == idempotency_key:
                return self._replay(existing_entry.order_id, payload)
            return self._adopt_entry(trigger, existing_entry, now=now, actor_id=actor_id)

        side = PositionSide.LONG if payload["side"] == "LONG" else PositionSide.SHORT
        bound = self._bound_recheck_verdict(trigger_id, decision.decision_id, payload)
        reference_price, mark_as_of = self._mark(self._opportunity(payload["opportunity_id"]))
        if bound is not None:
            # Submit strictly within the verdict the human saw on the card (S2).
            if bound.expires_at <= now:
                raise EntryRejected(
                    "CONFLICT", "the risk verdict has expired; run the re-check again"
                )
            verdict_payload = json.loads(bound.payload_json)
            risk_verdict_id = bound.risk_verdict_id
            verdict_value = str(verdict_payload.get("verdict"))
            binding_cap = str(verdict_payload.get("binding_cap"))
            allowed = verdict_payload.get("allowed") or {}
            requested = verdict_payload.get("requested") or {}
            try:
                allowed_quantity = _decimal_or_none(allowed.get("quantity"))
                allowed_leverage = float(allowed["leverage"])
                risk_amount_usd = Decimal(str(allowed["risk_amount_usd"]))
                risk_pct = float(requested["risk_pct"])
                stop_price = _decimal_or_none(requested.get("stop_price"))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise EntryRejected(
                    "CONFLICT",
                    f"the persisted risk verdict is invalid; run the re-check again: {exc}",
                ) from exc
        else:
            # Legacy/manually constructed trigger without a persisted re-check
            # verdict: recompute (never used by the production recheck path).
            try:
                evaluation = self._evaluate_and_persist_risk(
                    trigger_id=trigger_id,
                    trigger_payload=payload,
                    decision_id=decision.decision_id,
                    intent=intent,
                    seed=f"{trigger_id}:{decision.decision_id}:{idempotency_key}",
                )
            except RiskCapsError as exc:
                raise EntryRejected(exc.code, exc.message) from exc
            verdict = evaluation.verdict
            verdict_payload = evaluation.payload
            risk_verdict_id = evaluation.verdict_id
            reference_price = evaluation.reference_price
            mark_as_of = evaluation.mark_as_of
            verdict_value = verdict.verdict.value
            binding_cap = verdict.binding_cap.value
            allowed_quantity = verdict.allowed_quantity
            allowed_leverage = verdict.allowed_leverage
            risk_amount_usd = verdict.risk_amount_usd
            stop_price = verdict.stop_price
            risk_pct = float(verdict_payload["requested"]["risk_pct"])

        try:
            take_profits = tuple(Decimal(str(value)) for value in intent.get("take_profits", []))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise EntryRejected("ORDER_REJECTED", f"the entry intent is invalid: {exc}") from exc

        if verdict_value == "BLOCK" or allowed_quantity is None:
            self._update_trigger(
                trigger,
                status="BLOCKED",
                rejection_reason=f"risk blocked: {binding_cap}",
                risk_verdict=verdict_payload,
                expected_status=status,
                require_no_entry_order=True,
            )
            raise EntryRejected(
                "RISK_BLOCKED",
                f"risk gate blocked the entry ({binding_cap})",
            )
        approval_id = f"APR-{_digest(f'{trigger_id}:{idempotency_key}')}"
        order_intent = OrderIntent(
            symbol=payload["symbol"],
            side=OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=allowed_quantity,
            price=None,
            stop_price=stop_price,
            take_profits=take_profits,
            leverage=allowed_leverage,
            decision_id=decision.decision_id,
            risk_verdict_id=risk_verdict_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            position_side=side,
            risk_amount_usd=risk_amount_usd,
            risk_pct=risk_pct,
            trigger_id=trigger_id,
        )
        # Re-check under the write window: a concurrent accept may have
        # committed between our first lookup and here (equity then changed, so
        # the verdict binding would fail for no real reason).
        raced = self._store.get_entry_order_for_trigger(trigger_id)
        if raced is not None:
            if raced.idempotency_key == idempotency_key:
                return self._replay(raced.order_id, payload)
            return self._adopt_entry(trigger, raced, now=now, actor_id=actor_id)
        try:
            result = self._gateway.submit(
                order_intent,
                AuthContext(actor_id=actor_id, approval_id=approval_id),
                mark_price=reference_price,
                mark_as_of=mark_as_of,
            )
        except GatewayError as exc:
            # If an entry order exists for this trigger, it is the only order
            # this trigger may ever have: replay/adopt it instead of surfacing
            # a race-specific error (D6). A real block with no order still
            # raises below.
            entry = self._store.get_entry_order_for_trigger(trigger_id)
            if entry is not None:
                if entry.idempotency_key == idempotency_key:
                    return self._replay(entry.order_id, payload)
                return self._adopt_entry(trigger, entry, now=now, actor_id=actor_id)
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
            expected_status=status,
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
        """Reject a pending trigger and invalidate its opportunity.

        A committed entry order always wins: the crash window (order committed,
        trigger update lost) is refused with a typed ``CONFLICT`` instead of
        writing ``REJECTED`` over a live paper position. The write itself is a
        compare-and-set, so a concurrent accept cannot be clobbered.
        """
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        if self._store.get_entry_order_for_trigger(trigger_id) is not None:
            raise EntryRejected(
                "CONFLICT",
                f"an entry order already exists for trigger {trigger_id}; it cannot be rejected",
            )
        if payload["status"] == "ACCEPTED":
            raise EntryRejected("CONFLICT", "an accepted trigger cannot be rejected")
        previous_status = str(payload["status"])
        now = utc_timestamp(self._clock.now())
        payload["status"] = "REJECTED"
        payload["rejection_reason"] = reason
        human_action = {
            "action": "REJECT",
            "actor": {"kind": "USER", "id": actor_id},
            "at": now,
            "reason": reason,
            "confirmation_token_used": False,
        }
        payload["human_action"] = human_action
        record = replace(
            trigger,
            status="REJECTED",
            payload_json=json.dumps(payload, sort_keys=True),
        )
        self._write_trigger(
            record,
            expected_status=previous_status,
            require_no_order=True,
            require_no_entry_order=True,
        )
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
        """The reference mark and its timestamp."""
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

    def _evaluate_and_persist_risk(
        self,
        *,
        trigger_id: str,
        trigger_payload: dict[str, Any],
        decision_id: str,
        intent: dict[str, Any],
        seed: str,
    ) -> _RiskEvaluation:
        """Recompute the deterministic caps and persist the verdict (D2)."""
        opportunity = self._opportunity(trigger_payload["opportunity_id"])
        reference_price, mark_as_of = self._mark(opportunity)
        contract = self._store.get_symbol(trigger_payload["symbol"])
        if contract is None:
            # Fail closed: without tick/step/min-notional the gate would size and
            # round as if every filter were zero, which is unsafe.
            raise EntryRejected(
                "DATA_UNAVAILABLE",
                f"the symbol contract for {trigger_payload['symbol']} is missing",
            )
        now = utc_timestamp(self._clock.now())
        equity_stale: tuple[str, ...] = ()
        if self._paper is not None:
            summary = self._paper.summary()
            equity = Decimal(str(summary["equity"]))
            open_positions = int(summary["open_positions"])
            equity_stale = tuple(str(name) for name in summary.get("stale_inputs") or ())
        else:
            account = self._broker.account()
            positions = self._store.list_positions(state="OPEN", limit=None)
            equity, equity_view = compute_equity(
                Decimal(account.balance),
                [json.loads(record.payload_json) for record in positions],
            )
            open_positions = len(positions)
            equity_stale = equity_view.stale_inputs
        if equity_stale:
            # A degraded (cash-only) equity basis must be named as a gate input,
            # never silently used for percentage sizing (S7).
            equity_stale = tuple(dict.fromkeys((*equity_stale, "equity")))
        caps, _source = effective_caps(self._settings, self._store)
        side = str(trigger_payload["side"])
        try:
            # size_mode is exclusive: only the governing sizing field is read,
            # and a persisted intent that violates the mode is rejected rather
            # than silently sized by the other field.
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
                risk_pct = caps.per_trade_risk_pct
                notional = Decimal(str(intent["quote_notional_usd"]))
            else:
                raise EntryRejected("ORDER_REJECTED", f"unknown size_mode: {size_mode!r}")
            stop_price = Decimal(str(intent["stop_price"])) if intent.get("stop_price") else None
            leverage = float(intent["leverage"]) if intent.get("leverage") is not None else 1.0
            expected_slippage_bps = self._expected_slippage_bps(intent, reference_price)
            spread_bps = self._spread_bps(trigger_payload)
            # No realized-PnL source exists in this slice, so the daily-loss cap
            # is passed as unavailable and recorded as unevaluated instead of a
            # silent zero (D1).
            daily_loss_usd: Decimal | None = None
            verdict = evaluate_risk(
                request=RiskRequest(
                    symbol=trigger_payload["symbol"],
                    side=side,
                    entry_price=reference_price,
                    stop_price=stop_price,
                    quantity=None,
                    risk_pct=risk_pct,
                    leverage=leverage,
                    notional_usd=notional,
                ),
                caps=caps,
                equity=equity,
                open_positions=open_positions,
                daily_loss_usd=daily_loss_usd,
                mark_price=reference_price,
                mark_as_of=mark_as_of,
                now=now,
                tick_size=contract.tick_size,
                step_size=contract.step_size,
                min_notional=contract.min_notional,
                spread_bps=spread_bps,
                expected_slippage_bps=expected_slippage_bps,
                equity_stale_inputs=equity_stale,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # A malformed persisted intent must not surface as an unhandled 500.
            raise EntryRejected("ORDER_REJECTED", f"the entry intent is invalid: {exc}") from exc

        risk_verdict_id = f"RISK-{_digest(seed)}"
        requested_payload = {
            "entry_price": str(reference_price),
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
                decision_id=decision_id,
                verdict=verdict.verdict.value,
                binding_cap=verdict.binding_cap.value,
                computed_at=verdict.computed_at,
                expires_at=verdict.expires_at,
                payload_json=json.dumps(verdict_payload, sort_keys=True),
                spread_bps=spread_bps,
                expected_slippage_bps=expected_slippage_bps,
                daily_loss_usd=None if daily_loss_usd is None else str(daily_loss_usd),
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
        return _RiskEvaluation(
            verdict=verdict,
            payload=verdict_payload,
            verdict_id=risk_verdict_id,
            reference_price=reference_price,
            mark_as_of=mark_as_of,
        )

    def _expected_slippage_bps(
        self, intent: dict[str, Any], reference_price: Decimal
    ) -> float | None:
        """``|advisory entry - mark| / mark * 10000``; 0 when equal (D2).

        This is informational: it measures how far the LLM's advisory entry sits
        from the code-owned mark. It is not the broker's simulated fill
        slippage, which is the bounded ``OFT_PAPER_SLIPPAGE_BPS`` setting.
        """
        advisory = intent.get("entry_price")
        if advisory in (None, "") or reference_price <= 0:
            return None
        try:
            advisory_price = Decimal(str(advisory))
        except (ArithmeticError, TypeError, ValueError):
            return None
        return float(abs(advisory_price - reference_price) / reference_price * Decimal("10000"))

    def _spread_bps(self, trigger_payload: dict[str, Any]) -> float | None:
        """The spread measured in the trigger snapshot, else ``None`` (D2).

        Only the trigger snapshot is authoritative; a value in the report
        snapshot is not a substitute and is never silently trusted.

        The frozen trigger contract's snapshot carries no bid/ask/spread today,
        so production always returns ``None``: ``spread_bps`` is named in
        ``stale_inputs`` and the card reports ``data_quality=PARTIAL``. The 15
        bps cap is wired but inert until a live book feed supplies bid/ask (no
        contract change in this slice).
        """
        source = trigger_payload.get("snapshot")
        if not isinstance(source, dict):
            return None
        raw = source.get("spread_bps")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        bid = source.get("bid")
        ask = source.get("ask")
        if bid is not None and ask is not None:
            try:
                bid_price = Decimal(str(bid))
                ask_price = Decimal(str(ask))
            except (ArithmeticError, TypeError, ValueError):
                return None
            mid = (bid_price + ask_price) / Decimal("2")
            if bid_price > 0 and ask_price >= bid_price and mid > 0:
                return float((ask_price - bid_price) / mid * Decimal("10000"))
        return None

    def _data_quality(self, risk_stage: dict[str, Any], evaluated: bool) -> str:
        """PARTIAL whenever a named gate input is stale (honesty over optimism)."""
        stale = risk_stage.get("stale_inputs") or []
        if not evaluated or stale or risk_stage.get("error_code"):
            return "PARTIAL"
        return "COMPLETE"

    def _decision_card(
        self,
        *,
        trigger_id: str,
        trigger_payload: dict[str, Any],
        envelope: dict[str, Any],
        risk_stage: dict[str, Any],
        data_quality: str,
        error_code: str | None,
    ) -> dict[str, Any]:
        entry = envelope.get("entry_recheck") or {}
        decision_id = envelope.get("decision_id")
        return {
            "decision_id": decision_id,
            "trigger_id": trigger_id,
            "status": trigger_payload.get("status"),
            "rejection_reason": trigger_payload.get("rejection_reason"),
            "data_quality": data_quality,
            "error_code": error_code,
            "stage_order": ["TRIGGER", "LLM", "RISK", "HUMAN"],
            "trigger": {
                "trigger_id": trigger_id,
                "opportunity_id": trigger_payload.get("opportunity_id"),
                "symbol": trigger_payload.get("symbol"),
                "side": trigger_payload.get("side"),
                "status": trigger_payload.get("status"),
                "trigger_source": trigger_payload.get("trigger_source"),
                "triggered_at": trigger_payload.get("triggered_at"),
                "snapshot": trigger_payload.get("snapshot"),
                "condition_results": trigger_payload.get("condition_results", []),
                "provenance": "CALC",
            },
            "llm": {
                "decision_id": decision_id,
                "decision": entry.get("decision"),
                "validation_status": envelope.get("validation_status"),
                "confidence": envelope.get("confidence"),
                "rationale": envelope.get("rationale"),
                "reasons": entry.get("reasons", []),
                "provenance": "LLM",
            },
            "risk": risk_stage,
            "human_action": None,
            "entry_decision_id": decision_id,
        }

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
        return knowledge_injection(self._knowledge, PromptRole.ENTRY_RECHECK.value)

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
        knowledge_status: str | None,
        knowledge_reason: str | None,
    ) -> dict[str, Any]:
        amount_usd, price_version, unpriced = _total_cost(budgets)
        return {
            "provider": result.provider,
            "model": result.model,
            "model_snapshot": None,
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.version,
            "prompt_hash": rendered.prompt_hash,
            "knowledge_artifact_hash": knowledge_artifact_hash,
            "knowledge_status": knowledge_status,
            "knowledge_reason": knowledge_reason,
            "input_hash": digest,
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
        expected_status: str | None = None,
        require_no_order: bool = True,
        require_no_entry_order: bool = False,
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
        self._write_trigger(
            record,
            expected_status=expected_status,
            require_no_order=require_no_order,
            require_no_entry_order=require_no_entry_order,
        )
        return payload

    def _write_trigger(
        self,
        record: TriggerEventRecord,
        *,
        expected_status: str | None = None,
        require_no_order: bool = False,
        require_no_entry_order: bool = False,
    ) -> None:
        """Compare-and-set the trigger row; a lost race is a typed conflict.

        Nothing is audited by this method, so a zero-row write leaves the ledger
        untouched (M1).
        """
        updated = self._store.update_trigger_event(
            record,
            expected_status=expected_status,
            require_no_order=require_no_order,
            require_no_entry_order=require_no_entry_order,
        )
        if not updated:
            raise EntryRejected(
                "CONFLICT",
                "the trigger changed while the action was in flight; re-read and retry",
            )

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

    def _bound_recheck_verdict(
        self, trigger_id: str, decision_id: str, payload: dict[str, Any]
    ) -> RiskVerdictRecord | None:
        """The persisted verdict the human-approved card was rendered from (S2).

        The verdict id is deterministic (``trigger:decision:recheck``), so the
        binding does not require a new field on the closed trigger contract.
        """
        if not isinstance(payload.get("risk_verdict"), dict):
            return None
        verdict_id = f"RISK-{_digest(f'{trigger_id}:{decision_id}:recheck')}"
        record = self._store.get_risk_verdict(verdict_id)
        if record is None or record.decision_id != decision_id:
            raise EntryRejected(
                "CONFLICT",
                "the risk verdict for this re-check is not persisted; run the re-check again",
            )
        return record

    def _reconcile_recheck(self, trigger: TriggerEventRecord, entry: OrderRecord) -> dict[str, Any]:
        """Adopt a committed crash-window order from ``recheck`` (S6).

        No LLM call runs and the stored decision/verdict is never rewritten; the
        committed order and its position are surfaced in the card.
        """
        now = utc_timestamp(self._clock.now())
        result = self._adopt_entry(trigger, entry, now=now, actor_id="recheck")
        return self._reconciled_card(trigger.trigger_id, result)

    def _reconciled_card(self, trigger_id: str, result: EntryResult) -> dict[str, Any]:
        trigger = self._trigger(trigger_id)
        payload: dict[str, Any] = json.loads(trigger.payload_json)
        verdict = result.risk_verdict or {}
        stale = verdict.get("stale_inputs") or []
        risk_stage: dict[str, Any] = (
            {**verdict, "provenance": "CALC"}
            if verdict
            else {
                "verdict": "NOT_EVALUATED",
                "binding_cap": "NONE",
                "reasons": ["no risk verdict was persisted for the committed order"],
                "provenance": "CALC",
            }
        )
        return {
            "decision_id": result.decision_id,
            "trigger_id": trigger_id,
            "status": result.status,
            "rejection_reason": payload.get("rejection_reason"),
            "data_quality": "PARTIAL" if stale else "COMPLETE",
            "error_code": None,
            "stage_order": ["TRIGGER", "LLM", "RISK", "HUMAN"],
            "trigger": {
                "trigger_id": trigger_id,
                "opportunity_id": payload.get("opportunity_id"),
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "status": result.status,
                "trigger_source": payload.get("trigger_source"),
                "triggered_at": payload.get("triggered_at"),
                "snapshot": payload.get("snapshot"),
                "condition_results": payload.get("condition_results", []),
                "provenance": "CALC",
            },
            "llm": {
                "decision_id": result.decision_id,
                "decision": None,
                "validation_status": None,
                "confidence": None,
                "provenance": "LLM",
            },
            "risk": risk_stage,
            "human_action": payload.get("human_action"),
            "entry_decision_id": result.decision_id,
            "reconciled": True,
            "order": result.order,
            "fill": result.fill,
            "position": result.position,
            "account": result.account,
        }

    def _adopt_entry(
        self,
        trigger: TriggerEventRecord,
        entry: OrderRecord,
        *,
        now: str,
        actor_id: str,
    ) -> EntryResult:
        """Reconcile a committed order whose trigger update was lost (DB-1)."""
        if entry.environment != "PAPER" or entry.purpose != "ENTRY":
            raise EntryRejected(
                "CONFLICT",
                "the existing order is not a PAPER entry and cannot be adopted",
            )
        order_payload = json.loads(entry.payload_json)
        verdict_record = self._store.get_risk_verdict(
            str(order_payload.get("risk_verdict_id") or "")
        )
        if verdict_record is None:
            # Fail closed: the gateway refuses any order without a verdict, so an
            # adopted order must never surface as ACCEPTED with none (S9).
            raise EntryRejected(
                "CONFLICT",
                f"the risk verdict for order {entry.order_id} is missing; "
                "the committed order cannot be reconciled",
            )
        verdict_payload = json.loads(verdict_record.payload_json)
        human_action = {
            "action": "ACCEPT",
            "actor": {"kind": "USER", "id": actor_id},
            "at": now,
            "reason": "reconciled the already-committed paper order",
            "confirmation_token_used": False,
        }
        updated_payload = self._update_trigger(
            trigger,
            status="ACCEPTED",
            risk_verdict=verdict_payload,
            human_action=human_action,
            created_order_id=entry.order_id,
            entry_decision_id=entry.decision_id,
            expected_status=trigger.status,
        )
        record_audit(
            self._store,
            event_type="TRIGGER_ACCEPTED",
            actor={"kind": "USER", "id": actor_id},
            subject={"kind": "TRIGGER", "id": trigger.trigger_id},
            payload={
                "order_id": entry.order_id,
                "environment": "PAPER",
                "reconciled": True,
            },
            at=now,
        )
        return self._replay(entry.order_id, updated_payload)

    def _replay(self, order_id: str, trigger_payload: dict[str, Any]) -> EntryResult:
        order = self._store.get_order(order_id)
        if order is None:
            raise EntryRejected("ORDER_NOT_FOUND", f"Order {order_id} does not exist.")
        order_payload = json.loads(order.payload_json)
        verdict_id = str(order_payload.get("risk_verdict_id") or "")
        verdict_record = self._store.get_risk_verdict(verdict_id) if verdict_id else None
        if verdict_record is None:
            raise EntryRejected(
                "CONFLICT",
                f"the risk verdict for order {order_id} is missing; it cannot be replayed",
            )
        verdict_payload = json.loads(verdict_record.payload_json)
        position_id = order_payload.get("position_id")
        fill = self._store.get_fill_by_order_id(order_id)
        position = self._store.get_position(position_id) if position_id else None
        account = self._broker.account()
        return EntryResult(
            decision_id=str(trigger_payload.get("entry_decision_id") or order.decision_id or ""),
            risk_verdict=verdict_payload,
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
