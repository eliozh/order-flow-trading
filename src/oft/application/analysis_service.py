"""Analysis role: built-in prompt v1, decision persistence, opportunity drafts.

One canonical validation path: parse -> JSON Schema 2020-12 -> Pydantic ->
semantic checks -> exactly one repair -> VALID/REPAIRED/REJECTED. A rejected
output never creates an opportunity or an order (ADR-0005).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import Any

from oft.application.budget_ledger import BudgetEntry, BudgetLedger
from oft.application.knowledge_service import KnowledgeService, knowledge_injection
from oft.application.llm_orchestrator import LLMOrchestrator, LLMRuntimeRegistry
from oft.application.llm_validation import (
    ValidationIssue,
    ValidationOutcome,
    decision_envelope,
    hash_text,
    schema_text,
)
from oft.application.opportunity_service import OpportunityService
from oft.application.prompt_builder import KnowledgeInjection
from oft.application.prompt_service import (
    PromptService,
    RenderedPrompt,
    secret_values_from_settings,
)
from oft.config import Settings
from oft.domain.llm_output import DecisionOutput
from oft.domain.opportunities import ConditionDraftError, Opportunity
from oft.domain.prompts import (
    DEFAULT_SYSTEM_BODIES,
    DEFAULT_USER_BODIES,
    PromptRole,
    render,
    rendered_prompt_hash,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import (
    LLMPort,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMUsage,
    sanitize_provider_id,
)
from oft.ports.store import DecisionRecord, LLMCallRecord, StorePort

PROMPT_ID = "analysis"
PROMPT_VERSION = 1
PROMPT_HASH = hashlib.sha256(b"analysis-v1").hexdigest()
KNOWLEDGE_ARTIFACT_HASH: str | None = None


@dataclass(frozen=True)
class AnalysisOutcome:
    decision_id: str
    decision: dict[str, Any]
    validation_status: str
    output: DecisionOutput | None
    opportunity: Opportunity | None
    opportunity_ids: tuple[str, ...]
    llm_state: dict[str, Any]
    calls: tuple[LLMCallRecord, ...]
    budget: tuple[BudgetEntry, ...]


def render_analysis_prompt(
    *,
    symbol: str,
    timeframe: str,
    as_of: str,
    metrics: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[str, str]:
    """Render the built-in ANALYSIS v1 (used for previews and diagnostics)."""
    role = PromptRole.ANALYSIS
    system = render(DEFAULT_SYSTEM_BODIES[role], {})
    user = render(
        DEFAULT_USER_BODIES[role],
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of": as_of,
            "metrics": json.dumps(metrics, sort_keys=True, separators=(",", ":")),
            "snapshot": json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
            "knowledge": "",
            "schema": schema_text(),
        },
        role=role,
    )
    return system, user


def prompt_hash(
    system: str, user: str, *, prompt_id: str = PROMPT_ID, version: int = PROMPT_VERSION
) -> str:
    return rendered_prompt_hash(prompt_id, version, system, user)


def input_hash(system: str, user: str, *, extra: str = "") -> str:
    return hash_text(f"{system}\n{user}\n{schema_text()}\n{extra}")


def _reference_price(snapshot: dict[str, Any]) -> str:
    price = snapshot.get("last_price") or snapshot.get("mark_price")
    return str(price) if price is not None else "0"


class AnalysisService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: LLMPort,
        budget: BudgetLedger | None = None,
        settings: Settings | None = None,
        runtime: LLMRuntimeRegistry | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._provider = provider
        self._settings = settings
        self._budget = budget or BudgetLedger(store=store, settings=settings)
        self._opportunities = OpportunityService(store=store, clock=clock)
        self._secrets = secret_values_from_settings(settings) if settings is not None else ()
        self._prompts = PromptService(store=store, clock=clock, secret_values=self._secrets)
        self._knowledge = (
            KnowledgeService(settings=settings, clock=clock) if settings is not None else None
        )
        self._orchestrator = LLMOrchestrator(
            store=store,
            clock=clock,
            provider=provider,
            settings=settings,
            budget=self._budget,
            runtime=runtime,
        )

    def _knowledge_injection(self) -> KnowledgeInjection:
        """Mode A by default; a missing/inactive artifact is explicitly absent."""
        return knowledge_injection(self._knowledge, PromptRole.ANALYSIS.value)

    def analyze(
        self,
        *,
        symbol: str,
        timeframe: str,
        metrics: dict[str, Any],
        snapshot: dict[str, Any],
        scan_id: str,
        report_id: str,
    ) -> AnalysisOutcome:
        now = utc_timestamp(self._clock.now())
        knowledge = self._knowledge_injection()
        rendered = self._prompts.render_active(
            PromptRole.ANALYSIS,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "as_of": now,
                "metrics": json.dumps(metrics, sort_keys=True, separators=(",", ":")),
                "snapshot": json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                "knowledge": knowledge.text,
                "schema": schema_text(),
            },
            secret_values=self._secrets,
        )
        system, user = rendered.system, rendered.user
        digest = input_hash(system, user)
        decision_id = f"DEC-ANALYSIS-{digest[:16].upper()}"
        request = LLMRequest(
            role=LLMRole.ANALYSIS,
            model="fixture" if self._settings is None else "",
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
        orchestration = self._orchestrator.complete_with_validation(
            request=request,
            decision_id=decision_id,
            role=LLMRole.ANALYSIS,
        )
        first = orchestration.first_result
        outcome = orchestration.outcome
        calls = list(orchestration.calls)
        budgets = list(orchestration.budgets)

        provenance = self._provenance(
            result=first,
            digest=digest,
            rendered=rendered,
            calls=calls,
            budgets=budgets,
            now=now,
            knowledge_artifact_hash=knowledge.artifact_id,
            knowledge_status=knowledge.status,
            knowledge_reason=knowledge.reason,
        )
        subject = {
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of": now,
            "scan_id": scan_id,
            "report_id": report_id,
            "opportunity_id": None,
            "trigger_id": None,
            "position_id": None,
            "environment": "PAPER",
        }
        envelope = decision_envelope(
            decision_id=decision_id,
            role=LLMRole.ANALYSIS.value,
            output=outcome.output,
            subject=subject,
            provenance=provenance,
            validation_status=outcome.status,
            validation_errors=outcome.errors,
            rejected_reason=(None if outcome.valid else _rejection_reason(outcome)),
            raw_output_hash=outcome.raw_output_hash,
            persisted_at=now,
        )

        opportunity: Opportunity | None = None
        if (
            outcome.valid
            and outcome.output is not None
            and outcome.output.decision_type == "OPPORTUNITY_PROPOSAL"
            and outcome.output.opportunity is not None
        ):
            try:
                opportunity = self._opportunities.create_from_decision(
                    opportunity_draft=outcome.output.opportunity,
                    subject=subject,
                    decision_id=decision_id,
                    scan_id=scan_id,
                    report_id=report_id,
                    confidence=outcome.output.confidence,
                    provenance={
                        "llm": provenance,
                        "calc_snapshot_id": str(snapshot.get("snapshot_id", "")),
                        "calc_snapshot_hash": str(snapshot.get("snapshot_hash", "")),
                        "calc_metric_version": str(metrics.get("metric_version", "")),
                    },
                    data_quality=_data_quality(metrics, snapshot),
                    at=now,
                )
            except (ConditionDraftError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
                # ADR-0005: reject, never crash. A proposed opportunity that
                # cannot be materialized degrades to a REJECTED decision so the
                # scan still produces its CALC report.
                outcome = ValidationOutcome(
                    status="REJECTED",
                    output=None,
                    errors=(
                        ValidationIssue(
                            "opportunity",
                            "VALIDATION_FAILED",
                            f"the proposed opportunity could not be built: {exc}",
                        ),
                    ),
                    raw_output_hash=outcome.raw_output_hash,
                )
                envelope = decision_envelope(
                    decision_id=decision_id,
                    role=LLMRole.ANALYSIS.value,
                    output=None,
                    subject=subject,
                    provenance=provenance,
                    validation_status=outcome.status,
                    validation_errors=outcome.errors,
                    rejected_reason=_rejection_reason(outcome),
                    raw_output_hash=outcome.raw_output_hash,
                    persisted_at=now,
                )
                opportunity = None

        self._store.save_decision(
            DecisionRecord(
                decision_id=decision_id,
                role=LLMRole.ANALYSIS.value,
                decision_type=str(envelope["decision_type"]),
                validation_status=outcome.status,
                raw_output_hash=outcome.raw_output_hash,
                persisted_at=now,
                payload_json=json.dumps(envelope, sort_keys=True),
            )
        )

        llm_state = _llm_state(
            outcome=outcome,
            output=outcome.output,
            provenance=provenance,
            opportunity=opportunity,
            reference_price=_reference_price(snapshot),
            rendered=rendered,
            knowledge=knowledge,
        )
        return AnalysisOutcome(
            decision_id=decision_id,
            decision=envelope,
            validation_status=outcome.status,
            output=outcome.output,
            opportunity=opportunity,
            opportunity_ids=() if opportunity is None else (opportunity.opportunity_id,),
            llm_state=llm_state,
            calls=tuple(calls),
            budget=tuple(budgets),
        )

    def _provenance(
        self,
        *,
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
        cost_payload = budgets[0].payload if budgets else None
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
            "usage": json.loads(calls[0].usage_json) if calls else LLMUsage().to_contract(),
            "cost": {
                "amount_usd": cost_payload["cost_usd"] if cost_payload else "0",
                "currency": "USD",
                "price_version": cost_payload["price_version"] if cost_payload else "stub/v1",
                "unpriced": bool(cost_payload["unpriced"]) if cost_payload else True,
            },
            "recorded_at": now,
        }


def _rejection_reason(outcome: ValidationOutcome) -> str:
    if not outcome.errors:
        return "output rejected"
    first = outcome.errors[0]
    return f"{first.code}: {first.message}"


def _data_quality(metrics: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(metrics.get("data_quality", "COMPLETE")),
        "reasons": list(metrics.get("quality_reasons", [])),
        "gaps": list(metrics.get("gaps", [])),
        "last_event_at": snapshot.get("as_of"),
        "coverage_start": None,
    }


def _llm_state(
    *,
    outcome: ValidationOutcome,
    output: DecisionOutput | None,
    provenance: dict[str, Any],
    opportunity: Opportunity | None,
    reference_price: str,
    rendered: RenderedPrompt,
    knowledge: KnowledgeInjection,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "configured": True,
        "provider": provenance["provider"],
        "model": provenance["model"],
        "prompt_id": provenance["prompt_id"],
        "prompt_version": provenance["prompt_version"],
        "prompt_hash": provenance["prompt_hash"],
        "knowledge": knowledge.to_dict(),
        "system_prompt_id": rendered.system_prompt_id,
        "system_prompt_version": rendered.system_version,
        "system_prompt_hash": rendered.system_prompt_hash,
        "validation_status": outcome.status,
        "decision_type": output.decision_type if output is not None else None,
        "confidence": output.confidence if output is not None else None,
        "narrative": output.analysis.narrative if output and output.analysis else None,
        "rationale": output.rationale if output is not None else None,
        "opportunity_ids": [] if opportunity is None else [opportunity.opportunity_id],
        "cost": provenance["cost"],
        "reference_price": reference_price,
    }
    if outcome.valid:
        state["reason"] = None
    else:
        first = outcome.errors[0] if outcome.errors else ValidationIssue("output", "REJECTED", "")
        state["reason"] = f"{first.code}: {first.message}"
    return state


__all__ = [
    "AnalysisOutcome",
    "AnalysisService",
    "KNOWLEDGE_ARTIFACT_HASH",
    "PROMPT_HASH",
    "PROMPT_ID",
    "PROMPT_VERSION",
    "input_hash",
    "prompt_hash",
    "render_analysis_prompt",
]
