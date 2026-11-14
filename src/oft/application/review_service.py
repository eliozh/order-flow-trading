"""Advisory LLM trade review: one ``REVIEW`` decision linked from a trade log.

The review is advisory by construction. It reads the closed trade log, its fill,
and the bounded management history, renders the seeded ``REVIEW`` prompt, and
persists a schema-validated ``llm-decision`` envelope. It writes the
decisions/budget ledger, the orchestrator's ``llm_calls`` rows, the
``trade_logs.review_decision_id`` link, and a ``REVIEW_GENERATED`` audit. It
imports no execution gateway, paper broker, position mutation, or risk gate, and
it can never create an order, change a rule, apply a prompt edit, or alter a
risk cap. A rejected/refused output is persisted for audit but never linked, so
the trade metrics and any previous review link stay untouched.

Deliberate size exception: this module stays above the project's 800-line soft
ceiling because prompt construction, the admission/validation checks, the
read/replay state machine, and persistence/audit are one cohesive orchestration
unit with a single shared failure vocabulary. The read/replay block is the first
candidate for a future extraction; a split must not change behaviour.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn, get_args

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
)
from oft.application.llm_service_support import (
    ProviderSelectionError,
    bounded_history,
    decision_provenance,
    llm_error_code,
    resolve_provider_name,
    save_llm_decision,
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
    VALID_REVIEW_STATUSES,
    ReviewRead,
    ReviewRejected,
    ReviewResult,
    safe_identifier,
    validated_link_id,
    validated_review_fields,
    validated_validation_status,
)
from oft.config import Settings
from oft.domain.llm_output import Timeframe
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

_ALLOWED_TIMEFRAMES = frozenset(get_args(Timeframe))
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


_llm_error_code = llm_error_code
# Mirror the management prompt's bounded history so untrusted position data can
# never grow the review prompt without bound.
_bounded_history = bounded_history


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
            # A decision persisted by an earlier attempt whose link write failed
            # is orphaned: recover it instead of paying for a second provider
            # call. A refresh must still create a new decision.
            orphaned = self._orphaned_result(trade_id, record)
            if orphaned is not None:
                return orphaned

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
        if output is None:
            raise ReviewRejected("LLM_SCHEMA_INVALID", "the review output was rejected")
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
            rationale=output.rationale,
            caveats=tuple(output.caveats),
            confidence=output.confidence,
            validation_status=outcome.status,
            provenance=provenance,
        )

    # -- reads -----------------------------------------------------------

    def read(self, trade_id: str) -> ReviewRead:
        """The linked review for an existing trade, provider-free.

        A missing trade raises the typed ``NOT_FOUND`` (it is not a read state).
        Every failure after the trade exists returns a named ``MISSING`` or
        ``PARTIAL`` status instead of raising.
        """
        record = self._require_trade(trade_id)
        payload = _decode_payload(record.payload_json, subject=f"trade log {trade_id}")
        if payload is None:
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                reason="the trade log payload is unreadable",
            )
        linked = payload.get("review_decision_id")
        if linked is None:
            return ReviewRead(
                trade_id=trade_id,
                status="MISSING",
                reason="no advisory review is linked to this trade",
            )
        decision_id = validated_link_id(linked)
        if decision_id is None:
            # A present-but-malformed link id must be named, never echoed or
            # handed to the store (which would raise on the SQL bind).
            return ReviewRead(
                trade_id=trade_id,
                status="PARTIAL",
                reason="the linked review id is malformed",
            )
        return self._read_linked_review(trade_id, decision_id)

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
        if validation_status not in VALID_REVIEW_STATUSES:
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
        linked = validated_link_id(payload.get("review_decision_id"))
        if linked is None:
            return None
        return self._result_from_decision(trade_id, linked, idempotent_replay=True)

    def _existing_result(self, trade_id: str, decision_id: str) -> ReviewResult | None:
        if self._store.get_decision(decision_id) is None:
            return None
        return self._result_from_decision(trade_id, decision_id, idempotent_replay=True, link=True)

    def _orphaned_result(self, trade_id: str, record: TradeLogRecord) -> ReviewResult | None:
        """Recover an accepted REVIEW decision whose trade-log link was lost.

        The decision may have been persisted before the link write failed, so a
        retry with a fresh idempotency key would otherwise pay for a second
        provider call. Only decisions that authoritatively belong to this trade
        (same position, symbol, and environment) are candidates; malformed ones
        are skipped. The newest decision is considered first, and the envelope
        (not the stored column) is authoritative.
        """
        for decision in reversed(self._store.list_decisions(role=LLMRole.REVIEW.value)):
            envelope = _decode_payload(
                decision.payload_json, subject=f"decision {decision.decision_id}"
            )
            if envelope is None:
                continue
            if (
                envelope.get("role") != LLMRole.REVIEW.value
                or envelope.get("decision_type") != "REVIEW"
            ):
                continue
            if (
                validated_validation_status(envelope.get("validation_status"))
                not in VALID_REVIEW_STATUSES
            ):
                continue
            subject = envelope.get("subject")
            if not isinstance(subject, dict):
                continue
            if (
                subject.get("position_id") != record.position_id
                or subject.get("symbol") != record.symbol
                or subject.get("environment") != record.environment
            ):
                continue
            result = self._result_from_decision(
                trade_id, decision.decision_id, idempotent_replay=True, link=True
            )
            if result.status == "FOUND":
                _LOGGER.warning(
                    "review_orphan_adopted",
                    trade_id=trade_id,
                    decision_id=decision.decision_id,
                )
                return result
        return None

    def _partial_result(
        self,
        trade_id: str,
        decision_id: str,
        *,
        idempotent_replay: bool,
        validation_status: str | None,
        reason: str,
    ) -> ReviewResult:
        return ReviewResult(
            trade_id=trade_id,
            decision_id=decision_id,
            validation_status=validation_status,
            idempotent_replay=idempotent_replay,
            status="PARTIAL",
            reason=reason,
        )

    def _raise_replay_rejection(self, envelope: dict[str, Any]) -> NoReturn:
        # A tampered ``validation_errors`` value may be any JSON type; only a
        # list can be indexed. Never let it reach the response as a 500.
        errors = envelope.get("validation_errors")
        if not isinstance(errors, list):
            errors = []
        first = errors[0] if errors else {}
        first_code = first.get("code") if isinstance(first, dict) else None
        # The stored reason/field are untrusted text: only a safe string may be
        # echoed; otherwise use the code-owned fallback and drop the field.
        reason = (
            safe_identifier(envelope.get("rejected_reason")) or "the review decision was rejected"
        )
        field = safe_identifier(first.get("field")) if isinstance(first, dict) else None
        raise ReviewRejected(
            _llm_error_code(str(first_code or "")),
            reason,
            field=field,
            retryable=False,
        )

    def _load_review_envelope(self, decision_id: str) -> tuple[DecisionRecord, dict[str, Any]]:
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
        if (
            envelope.get("role") != LLMRole.REVIEW.value
            or envelope.get("decision_type") != "REVIEW"
        ):
            raise ReviewRejected("DATA_UNAVAILABLE", f"the decision {decision_id} is not a REVIEW")
        return decision, envelope

    def _stored_status_or_partial(
        self,
        trade_id: str,
        decision_id: str,
        envelope: dict[str, Any],
        *,
        idempotent_replay: bool,
    ) -> str | ReviewResult:
        """The accepted status, or the named PARTIAL for a non-FOUND envelope."""
        # The persisted envelope is authoritative: the DB column may be a stale
        # copy, so POST replay and GET must classify the same stored bytes.
        validation_status = validated_validation_status(envelope.get("validation_status"))
        if validation_status is None:
            return self._partial_result(
                trade_id,
                decision_id,
                idempotent_replay=idempotent_replay,
                validation_status=None,
                reason="the stored review has an unrecognized validation status",
            )
        if validation_status == "REJECTED":
            self._raise_replay_rejection(envelope)
        if validation_status not in VALID_REVIEW_STATUSES:
            return self._partial_result(
                trade_id,
                decision_id,
                idempotent_replay=idempotent_replay,
                validation_status=validation_status,
                reason="the linked review was rejected; no advisory text is available",
            )
        return validation_status

    def _result_from_decision(
        self,
        trade_id: str,
        decision_id: str,
        *,
        idempotent_replay: bool,
        link: bool = False,
    ) -> ReviewResult:
        decision, envelope = self._load_review_envelope(decision_id)
        status_or_partial = self._stored_status_or_partial(
            trade_id, decision_id, envelope, idempotent_replay=idempotent_replay
        )
        if isinstance(status_or_partial, ReviewResult):
            return status_or_partial
        fields = validated_review_fields(envelope)
        if fields is None:
            # A tampered stored row must never reach the response model as a
            # 500; report a named PARTIAL with no advisory text instead.
            return self._partial_result(
                trade_id,
                decision_id,
                idempotent_replay=idempotent_replay,
                validation_status=status_or_partial,
                reason="the stored review fields are malformed",
            )
        if link:
            self._link(trade_id, decision_id)
        # The decision, link, and audit are three independent writes. If the
        # audit failed after the link committed, a replay repairs it here; a
        # genuine audit failure still propagates.
        self._ensure_generated_audit(
            trade_id,
            decision_id,
            status_or_partial,
            fields["provenance"],
            decision.persisted_at,
        )
        return ReviewResult(
            trade_id=trade_id,
            decision_id=decision_id,
            rationale=fields["rationale"],
            caveats=fields["caveats"],
            confidence=fields["confidence"],
            validation_status=status_or_partial,
            provenance=fields["provenance"],
            idempotent_replay=idempotent_replay,
        )

    def _ensure_generated_audit(
        self,
        trade_id: str,
        decision_id: str,
        validation_status: str,
        provenance: dict[str, Any],
        now: str,
    ) -> None:
        """Emit the missing ``REVIEW_GENERATED`` audit for a persisted decision."""
        for event in self._store.list_audit_events_ascending(limit=None):
            if event.event_type != "REVIEW_GENERATED":
                continue
            try:
                body = json.loads(event.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # An unreadable existing event means the chain cannot be
                # verified. Appending a new event would silently duplicate the
                # audit, so fail closed instead (the payload body is not logged).
                _LOGGER.error(
                    "review_audit_event_unreadable",
                    seq=event.seq,
                    event_id=event.event_id,
                    error_type=type(exc).__name__,
                )
                raise ReviewRejected(
                    "DATA_UNAVAILABLE",
                    "the audit chain contains an unreadable event; "
                    "the review audit cannot be verified",
                ) from exc
            payload = body.get("payload") if isinstance(body, dict) else None
            if not isinstance(body, dict) or not isinstance(payload, dict):
                # A parseable but non-object body (or non-dict payload) cannot
                # be the code-written shape. Treat it like an unreadable event:
                # appending another event would silently duplicate the audit.
                self._raise_unreadable_audit(event)
            if payload.get("decision_id") == decision_id:
                return
        self._audit_generated(
            trade_id=trade_id,
            decision_id=decision_id,
            validation_status=validation_status,
            provenance=provenance,
            now=now,
        )

    def _raise_unreadable_audit(self, event: Any) -> NoReturn:
        """Log and fail closed on an existing event whose body is not an object."""
        # Only the identity is logged: the body is untrusted and unreadable.
        _LOGGER.error(
            "review_audit_event_unreadable",
            seq=event.seq,
            event_id=event.event_id,
        )
        raise ReviewRejected(
            "DATA_UNAVAILABLE",
            "the audit chain contains an unreadable event; the review audit cannot be verified",
        )

    # -- context and prompt ---------------------------------------------

    def _source_context(self, payload: dict[str, Any]) -> _SourceContext:
        """Resolve the report/scan timeframe exactly once for prompt and subject."""
        raw_signal = payload.get("signal")
        signal: dict[str, Any] = raw_signal if isinstance(raw_signal, dict) else {}
        # A tampered non-string or non-UTF-8 id must not reach the store's SQL
        # bind; ignore it and fall back to the report/scan ids alongside it.
        trigger_id = safe_identifier(signal.get("trigger_id"))
        scan_id = safe_identifier(signal.get("scan_id"))
        report_id = safe_identifier(signal.get("report_id"))
        opportunity_id = safe_identifier(signal.get("opportunity_id"))
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
                source_scan_id = safe_identifier(source.get("scan_id"))
                if source_scan_id is not None:
                    scan_id = source_scan_id
                source_report_id = safe_identifier(source.get("report_id"))
                if source_report_id is not None:
                    report_id = source_report_id
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
        assert timeframe is not None
        return _SourceContext(
            timeframe=timeframe,
            scan_id=scan_id,
            report_id=report_id,
            opportunity_id=opportunity_id,
            trigger_id=trigger_id,
        )

    def _review_context(self, record: TradeLogRecord, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the prompt's delimited DATA block from persisted records only.

        The review fails closed when one of its untrusted inputs is missing or
        unreadable: a review rendered from absent position/fill data would be
        persisted and linked as VALID while silently omitting the evidence it
        claims to assess.
        """
        trade_view = {field: payload.get(field) for field in _TRADE_FIELDS}
        position_record = self._store.get_position(record.position_id)
        if position_record is None:
            _LOGGER.warning("review_position_missing", position_id=record.position_id)
            raise ReviewRejected(
                "DATA_UNAVAILABLE",
                f"the position {record.position_id} for the trade is missing",
            )
        position_payload = _decode_payload(
            position_record.payload_json, subject=f"position {record.position_id}"
        )
        if position_payload is None:
            _LOGGER.warning("review_position_unreadable", position_id=record.position_id)
            raise ReviewRejected(
                "DATA_UNAVAILABLE",
                f"the position {record.position_id} payload is unreadable",
            )
        history = position_payload.get("management_history")
        fill_payload: dict[str, Any] | None = None
        if record.order_id:
            fill = self._store.get_fill_by_order_id(record.order_id)
            if fill is None:
                _LOGGER.warning("review_fill_missing", order_id=record.order_id)
                raise ReviewRejected(
                    "DATA_UNAVAILABLE",
                    f"the fill for order {record.order_id} is missing",
                )
            fill_payload = _decode_payload(fill.payload_json, subject=f"fill {fill.fill_id}")
            if fill_payload is None:
                _LOGGER.warning("review_fill_unreadable", fill_id=fill.fill_id)
                raise ReviewRejected(
                    "DATA_UNAVAILABLE",
                    f"the fill {fill.fill_id} payload is unreadable",
                )
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
        return decision_provenance(
            provider=result.provider,
            model=result.model,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            prompt_hash=rendered.prompt_hash,
            knowledge_artifact_hash=knowledge.artifact_id,
            knowledge_status=knowledge.status,
            knowledge_reason=knowledge.reason,
            input_hash=input_hash,
            request_id=sanitize_provider_id(result.request_id, self._secrets),
            latency_ms=result.latency_ms,
            usage=total_usage(calls),
            amount_usd=amount_usd,
            price_version=price_version,
            unpriced=unpriced,
            recorded_at=now,
        )

    # -- persistence -----------------------------------------------------

    def _save_decision(
        self,
        decision_id: str,
        validation_status: str,
        raw_output_hash: str,
        envelope: dict[str, Any],
        now: str,
    ) -> None:
        save_llm_decision(
            self._store,
            decision_id=decision_id,
            role=LLMRole.REVIEW.value,
            decision_type="REVIEW",
            validation_status=validation_status,
            raw_output_hash=raw_output_hash,
            envelope=envelope,
            now=now,
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
        try:
            return resolve_provider_name(
                self._provider,
                self._settings,
                requested,
                role_label="review",
            )
        except ProviderSelectionError as exc:
            raise ReviewRejected(exc.code, exc.message) from exc


__all__ = [
    "ADVISORY_LABEL",
    "ReviewRead",
    "ReviewRejected",
    "ReviewResult",
    "ReviewService",
]
