"""Advisory LLM trade review: one ``REVIEW`` decision linked from a trade log.

The review is advisory by construction. It reads the closed trade log, its fill,
and the bounded management history, renders the seeded ``REVIEW`` prompt, and
persists a schema-validated ``llm-decision`` envelope. It writes only the
decisions/budget ledger and the ``trade_logs.review_decision_id`` link; it
imports no execution gateway, paper broker, position mutation, or risk gate, and
it can never create an order, change a rule, apply a prompt edit, or alter a
risk cap. A rejected/refused output is persisted for audit but never linked, so
the trade metrics and any previous review link stay untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn

import structlog

from oft.application.budget_ledger import BudgetLedger
from oft.application.budget_service import BudgetEntry
from oft.application.kill_switch import record_audit
from oft.application.knowledge_service import KnowledgeService, knowledge_injection
from oft.application.llm_orchestrator import (
    LLMConfigurationError,
    LLMOrchestrator,
    LLMRuntimeRegistry,
    OrchestrationResult,
    provider_configured,
)
from oft.application.llm_usage import total_cost, total_usage
from oft.application.llm_validation import (
    ValidationOutcome,
    decision_envelope,
    hash_text,
    schema_text,
)
from oft.application.prompt_builder import KnowledgeInjection, sanitize_knowledge_text
from oft.application.prompt_service import (
    PromptService,
    PromptVersionMissingError,
    RenderedPrompt,
    secret_values_from_settings,
)
from oft.application.review_models import (
    ADVISORY_LABEL,
    ReviewRead,
    ReviewRejected,
    ReviewResult,
    validated_review_fields,
    validated_validation_status,
)
from oft.config import Settings
from oft.domain.prompts import PromptRole
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import (
    LLMAdapterError,
    LLMPort,
    LLMRequest,
    LLMResult,
    LLMRole,
    sanitize_provider_id,
)
from oft.ports.store import (
    DecisionRecord,
    LLMCallRecord,
    StorePort,
    TradeLogRecord,
)

_LOGGER = structlog.get_logger(__name__)

_LLM_ERROR_CODES = frozenset({"LLM_REFUSED", "LLM_OUTPUT_TRUNCATED", "LLM_SCHEMA_INVALID"})
_VALID_REVIEW_STATUSES = frozenset({"VALID", "REPAIRED"})
_ALLOWED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h", "4h", "8h", "1d"})
# Mirror the management prompt's bounded history so untrusted position data can
# never grow the review prompt without bound.
_HISTORY_MAX_ENTRIES = 20
_HISTORY_TEXT_LIMIT = 200
_HISTORY_MAX_BYTES = 4096
_RETRY_HINT = "Retry the review to attempt again."
_TRADE_FIELDS = (
    "trade_id",
    "position_id",
    "environment",
    "symbol",
    "side",
    "opened_at",
    "closed_at",
    "entry_avg_price",
    "exit_avg_price",
    "size",
    "gross_pnl_usd",
    "fees_usd",
    "funding_usd",
    "net_pnl_usd",
    "return_pct",
    "r_multiple",
    "mae_usd",
    "mfe_usd",
    "outcome_tags",
    "decision_quality",
    "data_quality",
    "provenance",
)


@dataclass(frozen=True)
class _SourceContext:
    timeframe: str
    scan_id: str | None
    report_id: str | None
    opportunity_id: str | None
    trigger_id: str | None


def _review_decision_id(trade_id: str, idempotency_key: str) -> str:
    """Deterministic, code-owned id; a caller can never supply one."""
    digest = hashlib.sha256(f"{trade_id}:{idempotency_key}".encode()).hexdigest()[:16]
    return f"RVW-{digest.upper()}"


def _llm_error_code(code: str | None) -> str:
    return code if code in _LLM_ERROR_CODES else "LLM_SCHEMA_INVALID"


def _decode_payload(raw: str, *, subject: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # The cause is logged without the payload body: corrupt trade/decision
        # data is untrusted and may contain secrets or hostile text.
        _LOGGER.warning(
            "could not decode a review payload",
            subject=subject,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return payload if isinstance(payload, dict) else None


def _neutralize_prompt_value(value: Any) -> Any:
    """Escape untrusted review text so it stays inert inside ``<position>``.

    The knowledge builder already neutralizes ``<``/``>``; the trade, fill, and
    management-history views are equally untrusted, so the same treatment is
    applied recursively before the context is JSON-encoded.
    """
    if isinstance(value, str):
        return sanitize_knowledge_text(value)
    if isinstance(value, dict):
        return {
            sanitize_knowledge_text(key) if isinstance(key, str) else key: (
                _neutralize_prompt_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_neutralize_prompt_value(item) for item in value]
    return value


def _bounded_history(value: object) -> list[dict[str, Any]]:
    """The newest bounded management-history entries for the prompt."""
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value[-_HISTORY_MAX_ENTRIES:]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        for key in ("note", "rationale"):
            text = entry.get(key)
            if isinstance(text, str) and len(text) > _HISTORY_TEXT_LIMIT:
                entry[key] = text[:_HISTORY_TEXT_LIMIT]
        entries.append(entry)
    while entries and (
        len(json.dumps(entries, sort_keys=True).encode("utf-8")) > _HISTORY_MAX_BYTES
    ):
        entries.pop(0)
    return entries


class ReviewService:
    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        settings: Settings,
        provider: LLMPort | None,
        runtime: LLMRuntimeRegistry | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
        self._provider = provider
        self._secrets = secret_values_from_settings(settings)
        self._prompts = PromptService(store=store, clock=clock, secret_values=self._secrets)
        self._knowledge = KnowledgeService(settings=settings, clock=clock)
        self._orchestrator = (
            LLMOrchestrator(
                store=store,
                clock=clock,
                provider=provider,
                settings=settings,
                budget=BudgetLedger(store=store, settings=settings),
                runtime=runtime,
            )
            if provider is not None
            else None
        )

    @property
    def configured(self) -> bool:
        return self._provider is not None

    # -- writes ----------------------------------------------------------

    def review(
        self,
        trade_id: str,
        *,
        refresh: bool = False,
        idempotency_key: str,
        provider_name: str | None = None,
    ) -> ReviewResult:
        key = self._require_key(idempotency_key)
        record = self._require_trade(trade_id)
        payload = self._decode_trade(record)

        if not refresh:
            linked = self._linked_result(trade_id, payload)
            if linked is not None:
                return linked

        decision_id = _review_decision_id(trade_id, key)
        existing = self._existing_result(trade_id, decision_id)
        if existing is not None:
            # Same key (or a previously persisted decision for this key) replays
            # the stored result; a refresh needs a fresh key to supersede it.
            return existing

        self._provider_name(provider_name)
        source = self._source_context(payload)
        context = self._review_context(record, payload)
        knowledge = self._knowledge_injection()
        rendered, request = self._request(record, source, context, knowledge)
        orchestration = self._complete(decision_id, request)

        now = utc_timestamp(self._clock.now())
        provenance = self._provenance(
            orchestration.final_result,
            rendered,
            request.input_hash,
            list(orchestration.calls),
            list(orchestration.budgets),
            now,
            knowledge,
        )
        subject = self._subject(record, source, now)
        if not orchestration.outcome.valid:
            self._raise_rejection(decision_id, subject, provenance, orchestration.outcome, now)
        return self._persist_accepted(
            trade_id, decision_id, subject, provenance, orchestration.outcome, now
        )

    def _require_key(self, idempotency_key: str) -> str:
        key = (idempotency_key or "").strip()
        if not key:
            raise ReviewRejected("IDEMPOTENCY_KEY_REQUIRED", "a review requires an Idempotency-Key")
        return key

    def _require_trade(self, trade_id: str) -> TradeLogRecord:
        record = self._store.get_trade_log(trade_id)
        if record is None:
            raise ReviewRejected("NOT_FOUND", f"Trade {trade_id} does not exist.")
        return record

    def _complete(self, decision_id: str, request: LLMRequest) -> OrchestrationResult:
        orchestrator = self._orchestrator
        assert orchestrator is not None
        try:
            return orchestrator.complete_with_validation(
                request=request,
                decision_id=decision_id,
                role=LLMRole.REVIEW,
            )
        except (LLMConfigurationError, LLMAdapterError) as exc:
            raise ReviewRejected(exc.code, exc.message) from exc

    def _raise_rejection(
        self,
        decision_id: str,
        subject: dict[str, Any],
        provenance: dict[str, Any],
        outcome: ValidationOutcome,
        now: str,
    ) -> NoReturn:
        """Persist the rejection under an attempt id, then raise it for the caller.

        The attempt-specific id keeps the rejection auditable without occupying
        the deterministic decision id, so a later plain retry is not
        short-circuited by the earlier failure.
        """
        errors = outcome.errors
        rejected_id = self._rejected_decision_id(decision_id)
        envelope = decision_envelope(
            decision_id=rejected_id,
            role=LLMRole.REVIEW.value,
            output=None,
            subject=subject,
            provenance=provenance,
            validation_status="REJECTED",
            validation_errors=errors,
            rejected_reason=(
                f"{errors[0].code}: {errors[0].message}" if errors else "invalid output"
            ),
            raw_output_hash=outcome.raw_output_hash,
            persisted_at=now,
            decision_type="REVIEW",
        )
        self._save_decision(rejected_id, "REJECTED", outcome.raw_output_hash, envelope, now)
        first = errors[0] if errors else None
        raise ReviewRejected(
            _llm_error_code(first.code if first is not None else None),
            (first.message if first is not None else "the review output was rejected")
            + f" {_RETRY_HINT}",
            field=first.field if first is not None else None,
        )

    def _rejected_decision_id(self, decision_id: str) -> str:
        prefix = f"{decision_id}-R"
        attempts = sum(
            1
            for record in self._store.list_decisions(role=LLMRole.REVIEW.value)
            if record.decision_id.startswith(prefix)
        )
        return f"{prefix}{attempts + 1}"

    def _persist_accepted(
        self,
        trade_id: str,
        decision_id: str,
        subject: dict[str, Any],
        provenance: dict[str, Any],
        outcome: ValidationOutcome,
        now: str,
    ) -> ReviewResult:
        output = outcome.output
        assert output is not None  # ``valid`` guarantees a parsed output
        envelope = decision_envelope(
            decision_id=decision_id,
            role=LLMRole.REVIEW.value,
            output=output,
            subject=subject,
            provenance=provenance,
            validation_status=outcome.status,
            validation_errors=outcome.errors,
            rejected_reason=None,
            raw_output_hash=outcome.raw_output_hash,
            persisted_at=now,
            decision_type="REVIEW",
        )
        self._save_decision(decision_id, outcome.status, outcome.raw_output_hash, envelope, now)
        self._link(trade_id, decision_id)
        self._audit_generated(
            trade_id=trade_id,
            decision_id=decision_id,
            validation_status=outcome.status,
            provenance=provenance,
            now=now,
        )
        return ReviewResult(
            trade_id=trade_id,
            decision_id=decision_id,
            rationale=envelope.get("rationale") or "",
            caveats=tuple(str(item) for item in envelope.get("caveats") or []),
            confidence=float(envelope.get("confidence") or 0.0),
            validation_status=outcome.status,
            provenance=provenance,
        )

    # -- reads -----------------------------------------------------------

    def read(self, trade_id: str) -> ReviewRead:
        """The linked review, provider-free; every failure is a named status."""
        record = self._require_trade(trade_id)
        payload = _decode_payload(record.payload_json, subject=f"trade log {trade_id}")
        if payload is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                reason="the trade log payload is unreadable",
            )
        linked = payload.get("review_decision_id")
        if not isinstance(linked, str) or not linked.strip():
            return ReviewRead(
                trade_id=trade_id,
                status="MISSING",
                reason="no advisory review is linked to this trade",
            )
        return self._read_linked_review(trade_id, linked)

    def _read_linked_review(self, trade_id: str, linked: str) -> ReviewRead:
        decision = self._store.get_decision(linked)
        if decision is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                reason=f"the linked review decision {linked} is missing",
            )
        envelope = _decode_payload(decision.payload_json, subject=f"decision {linked}")
        if envelope is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                reason=f"the linked review decision {linked} is unreadable",
            )
        if (
            envelope.get("role") != LLMRole.REVIEW.value
            or envelope.get("decision_type") != "REVIEW"
        ):
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                reason="the linked decision is not a REVIEW",
            )
        validation_status = validated_validation_status(envelope.get("validation_status"))
        if validation_status is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                reason="the linked review has an unrecognized validation status",
            )
        if validation_status not in _VALID_REVIEW_STATUSES:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                validation_status=validation_status,
                reason="the linked review was rejected; no advisory text is available",
            )
        fields = validated_review_fields(envelope)
        if fields is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                decision_id=linked,
                validation_status=validation_status,
                reason="the linked review fields are malformed",
            )
        return ReviewRead(trade_id=trade_id, status="FOUND", decision_id=linked, **fields)

    # -- replay helpers --------------------------------------------------

    def _decode_trade(self, record: TradeLogRecord) -> dict[str, Any]:
        payload = _decode_payload(record.payload_json, subject=f"trade log {record.trade_id}")
        if payload is None:
            raise ReviewRejected(
                "DATA_UNAVAILABLE",
                f"the trade log {record.trade_id} payload is unreadable",
            )
        return payload

    def _linked_result(self, trade_id: str, payload: dict[str, Any]) -> ReviewResult | None:
        linked = payload.get("review_decision_id")
        if not isinstance(linked, str) or not linked.strip():
            return None
        return self._result_from_decision(trade_id, linked, idempotent_replay=True)

    def _existing_result(self, trade_id: str, decision_id: str) -> ReviewResult | None:
        if self._store.get_decision(decision_id) is None:
            return None
        return self._result_from_decision(trade_id, decision_id, idempotent_replay=True, link=True)

    def _result_from_decision(
        self,
        trade_id: str,
        decision_id: str,
        *,
        idempotent_replay: bool,
        link: bool = False,
    ) -> ReviewResult:
        decision = self._store.get_decision(decision_id)
        if decision is None:
            raise ReviewRejected(
                "DATA_UNAVAILABLE", f"the review decision {decision_id} is missing"
            )
        envelope = _decode_payload(decision.payload_json, subject=f"decision {decision_id}")
        if envelope is None:
            raise ReviewRejected(
                "DATA_UNAVAILABLE", f"the review decision {decision_id} is unreadable"
            )
        if decision.validation_status not in _VALID_REVIEW_STATUSES:
            errors = envelope.get("validation_errors") or []
            first = errors[0] if errors else {}
            first_code = first.get("code") if isinstance(first, dict) else None
            raise ReviewRejected(
                _llm_error_code(str(first_code or "")),
                str(envelope.get("rejected_reason") or "the review decision was rejected"),
                field=first.get("field") if isinstance(first, dict) else None,
                retryable=False,
            )
        if (
            envelope.get("role") != LLMRole.REVIEW.value
            or envelope.get("decision_type") != "REVIEW"
        ):
            raise ReviewRejected("DATA_UNAVAILABLE", f"the decision {decision_id} is not a REVIEW")
        fields = validated_review_fields(envelope)
        if fields is None:
            # A tampered stored row must never reach the response model as a
            # 500; report a named PARTIAL with no advisory text instead.
            return ReviewResult(
                trade_id=trade_id,
                decision_id=decision_id,
                validation_status=str(decision.validation_status),
                idempotent_replay=idempotent_replay,
                status="PARTIAL",
                reason="the stored review fields are malformed",
            )
        if link:
            self._link(trade_id, decision_id)
        return ReviewResult(
            trade_id=trade_id,
            decision_id=decision_id,
            rationale=fields["rationale"],
            caveats=fields["caveats"],
            confidence=fields["confidence"],
            validation_status=str(decision.validation_status),
            provenance=fields["provenance"],
            idempotent_replay=idempotent_replay,
        )

    # -- context and prompt ---------------------------------------------

    def _source_context(self, payload: dict[str, Any]) -> _SourceContext:
        """Resolve the report/scan timeframe exactly once for prompt and subject."""
        raw_signal = payload.get("signal")
        signal: dict[str, Any] = raw_signal if isinstance(raw_signal, dict) else {}
        trigger_id = signal.get("trigger_id")
        scan_id = signal.get("scan_id") if isinstance(signal.get("scan_id"), str) else None
        report_id = signal.get("report_id") if isinstance(signal.get("report_id"), str) else None
        opportunity_id = (
            signal.get("opportunity_id") if isinstance(signal.get("opportunity_id"), str) else None
        )
        trigger = self._store.get_trigger_event(trigger_id) if trigger_id else None
        if trigger is not None:
            opportunity_id = trigger.opportunity_id
            opportunity = self._store.get_opportunity(opportunity_id)
            if opportunity is not None:
                opportunity_payload = _decode_payload(
                    opportunity.payload_json, subject=f"opportunity {opportunity_id}"
                )
                raw_source = (
                    opportunity_payload.get("source")
                    if isinstance(opportunity_payload, dict)
                    else None
                )
                source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
                if isinstance(source.get("scan_id"), str):
                    scan_id = source["scan_id"]
                if isinstance(source.get("report_id"), str):
                    report_id = source["report_id"]
        timeframe: str | None = None
        if report_id:
            report = self._store.get_report(report_id)
            if report is not None and report.timeframe:
                timeframe = report.timeframe
        if timeframe is None and scan_id:
            scan = self._store.get_scan(scan_id)
            if scan is not None and scan.period:
                timeframe = scan.period
        if timeframe not in _ALLOWED_TIMEFRAMES:
            raise ReviewRejected(
                "DATA_UNAVAILABLE",
                "the trade has no source report timeframe for the review prompt",
            )
        return _SourceContext(
            timeframe=timeframe,
            scan_id=scan_id,
            report_id=report_id,
            opportunity_id=opportunity_id,
            trigger_id=trigger_id if isinstance(trigger_id, str) else None,
        )

    def _review_context(self, record: TradeLogRecord, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the prompt's delimited DATA block from persisted records only."""
        trade_view = {field: payload.get(field) for field in _TRADE_FIELDS}
        position_record = self._store.get_position(record.position_id)
        position_payload = (
            _decode_payload(position_record.payload_json, subject=f"position {record.position_id}")
            if position_record is not None
            else None
        )
        history = (
            position_payload.get("management_history")
            if isinstance(position_payload, dict)
            else None
        )
        fill_payload: dict[str, Any] | None = None
        if record.order_id:
            fill = self._store.get_fill_by_order_id(record.order_id)
            if fill is not None:
                fill_payload = _decode_payload(fill.payload_json, subject=f"fill {fill.fill_id}")
        return {
            "trade": trade_view,
            "fill": fill_payload,
            "management_history": _bounded_history(history),
        }

    def _request(
        self,
        record: TradeLogRecord,
        source: _SourceContext,
        context: dict[str, Any],
        knowledge: KnowledgeInjection,
    ) -> tuple[RenderedPrompt, LLMRequest]:
        try:
            rendered = self._prompts.render_active(
                PromptRole.REVIEW,
                {
                    "symbol": record.symbol,
                    "timeframe": source.timeframe,
                    # Untrusted trade/fill/history text is escaped before it is
                    # JSON-encoded, so it cannot close the <position> delimiter.
                    "position": json.dumps(_neutralize_prompt_value(context), sort_keys=True),
                    "schema": schema_text(),
                    "knowledge": knowledge.text,
                },
                secret_values=self._secrets,
            )
        except PromptVersionMissingError as exc:
            raise ReviewRejected("PROMPT_VERSION_MISSING", exc.message) from exc
        digest = hash_text(f"{rendered.system}\n{rendered.user}\n{schema_text()}")
        request = LLMRequest(
            role=LLMRole.REVIEW,
            model="",
            system_prompt=rendered.system,
            user_prompt=rendered.user,
            schema_json=schema_text(),
            input_hash=digest,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_hash=rendered.prompt_hash,
            request_id=f"REQ-RVW-{digest[:12].upper()}",
            system_prompt_id=rendered.system_prompt_id,
            system_prompt_version=rendered.system_version,
            system_prompt_hash=rendered.system_prompt_hash,
        )
        return rendered, request

    def _knowledge_injection(self) -> KnowledgeInjection:
        return knowledge_injection(self._knowledge, PromptRole.REVIEW.value)

    def _subject(self, record: TradeLogRecord, source: _SourceContext, now: str) -> dict[str, Any]:
        environment = record.environment
        if environment not in {"PAPER", "LIVE"}:
            raise ReviewRejected(
                "DATA_UNAVAILABLE",
                f"the trade environment {environment!r} is not a contract environment",
            )
        return {
            "symbol": record.symbol,
            "timeframe": source.timeframe,
            "as_of": now,
            "scan_id": source.scan_id,
            "report_id": source.report_id,
            "opportunity_id": source.opportunity_id,
            "trigger_id": source.trigger_id,
            "position_id": record.position_id,
            "environment": environment,
        }

    def _provenance(
        self,
        result: LLMResult,
        rendered: RenderedPrompt,
        input_hash: str,
        calls: list[LLMCallRecord],
        budgets: list[BudgetEntry],
        now: str,
        knowledge: KnowledgeInjection,
    ) -> dict[str, Any]:
        amount_usd, price_version, unpriced = total_cost(budgets)
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
            "usage": total_usage(calls),
            "cost": {
                "amount_usd": amount_usd,
                "currency": "USD",
                "price_version": price_version,
                "unpriced": unpriced,
            },
            "recorded_at": now,
        }

    # -- persistence -----------------------------------------------------

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
                role=LLMRole.REVIEW.value,
                decision_type="REVIEW",
                validation_status=validation_status,
                raw_output_hash=raw_output_hash,
                persisted_at=now,
                payload_json=json.dumps(envelope, sort_keys=True, allow_nan=False),
            )
        )

    def _link(self, trade_id: str, decision_id: str) -> None:
        try:
            linked = self._store.link_trade_log_review(trade_id, decision_id=decision_id)
        except ValueError as exc:
            raise ReviewRejected("DATA_UNAVAILABLE", str(exc)) from exc
        if not linked:
            raise ReviewRejected(
                "DATA_UNAVAILABLE", f"the trade log {trade_id} disappeared before the link"
            )

    def _audit_generated(
        self,
        *,
        trade_id: str,
        decision_id: str,
        validation_status: str,
        provenance: dict[str, Any],
        now: str,
    ) -> None:
        record_audit(
            self._store,
            event_type="REVIEW_GENERATED",
            actor={"kind": "SYSTEM", "id": "review_service"},
            subject={"kind": "TRADE_LOG", "id": trade_id},
            payload={
                "decision_id": decision_id,
                "role": LLMRole.REVIEW.value,
                "decision_type": "REVIEW",
                "validation_status": validation_status,
                "provider": provenance.get("provider"),
                "model": provenance.get("model"),
                "prompt_id": provenance.get("prompt_id"),
                "prompt_version": provenance.get("prompt_version"),
                "prompt_hash": provenance.get("prompt_hash"),
                "knowledge_status": provenance.get("knowledge_status"),
                "advisory": True,
            },
            at=now,
        )

    def _provider_name(self, requested: str | None) -> str:
        value = (requested or "").strip().lower()
        if self._provider is None:
            raise ReviewRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                "No LLM provider is configured; the review cannot run.",
            )
        name = value or str(getattr(self._provider, "provider", "") or "")
        if not name:
            capabilities = getattr(self._provider, "capabilities", None)
            if callable(capabilities):
                name = str(getattr(capabilities(), "provider", "") or "")
        if requested and name != requested.strip().lower():
            raise ReviewRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"No {requested} provider is configured for the review role.",
            )
        if not provider_configured(self._settings, name):
            raise ReviewRejected(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"No {name or 'LLM'} API key is configured; the review cannot run.",
            )
        return name


__all__ = [
    "ADVISORY_LABEL",
    "ReviewRead",
    "ReviewRejected",
    "ReviewResult",
    "ReviewService",
]
