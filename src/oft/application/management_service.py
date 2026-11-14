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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

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
from oft.application.llm_usage import total_cost, total_usage
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
    PositionCloseError,
    PositionNotFound,
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
from oft.domain.oms import Position, PositionSide
from oft.domain.pnl import json_decimal
from oft.domain.positions import PositionError, position_from_contract
from oft.domain.prompts import PromptRole
from oft.domain.risk import (
    BindingCap,
    ManagementAction,
    ManagementClassification,
    ManagementStatus,
    StopChange,
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
    sanitize_provider_id,
)
from oft.ports.store import (
    DecisionRecord,
    LLMCallRecord,
    OrderRecord,
    PositionRecord,
    StorePort,
    TradeLogRecord,
)

_STOP_REFUSAL_FIELD = "position_management.new_stop_price"
# The prompt embeds prior management notes; cap the count, each free-text note,
# and the serialized total so a long-lived position cannot grow the prompt
# without bound.
_MANAGEMENT_HISTORY_MAX_ENTRIES = 20
_MANAGEMENT_HISTORY_TEXT_LIMIT = 200
_MANAGEMENT_HISTORY_MAX_BYTES = 4096


class ManagementRejected(Exception):
    """A typed management failure; ``code`` maps to the API error catalog."""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class ManagementRefusal:
    """One typed refusal attached to an executed management result.

    The stop-loosening guard is independent of the reduce-only execution: the
    close may succeed while its companion stop change is refused. The typed
    shape means the audit writer and the API response can never read a missing
    key, and ``to_contract`` keeps the wire format identical to the historical
    ``{code, field, message}`` dicts.
    """

    code: str
    field: str
    message: str

    def to_contract(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class ManagementResult:
    """The outcome of one ``oft pos manage`` / API management call."""

    position_id: str
    decision_id: str
    action: ManagementAction
    status: ManagementStatus
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
    # Additive typed refusals (for example a PARTIAL_CLOSE whose companion
    # stop-loosening was refused while the reduce-only close still executed).
    refusals: tuple[ManagementRefusal, ...] = ()
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        # Coerce the closed vocabularies so callers may pass their string values
        # while the result still cannot carry an unknown action/status.
        object.__setattr__(self, "action", ManagementAction(self.action))
        object.__setattr__(self, "status", ManagementStatus(self.status))
        if not self.reason_code.strip():
            raise ValueError("a management result requires a non-empty reason_code")
        if self.status is ManagementStatus.EXECUTED:
            if not self.executed:
                raise ValueError("an EXECUTED result must set executed=True")
            if self.order is None or self.fill is None:
                raise ValueError("an EXECUTED result requires its order and fill")
            if self.action is ManagementAction.FULL_CLOSE and self.trade_log is None:
                raise ValueError("an EXECUTED FULL_CLOSE requires its trade log")
        else:
            if self.executed:
                raise ValueError("a non-executed result must not set executed=True")
            if self.order is not None or self.fill is not None:
                raise ValueError("a non-executed result must not carry an order or fill")
            if self.trade_log is not None:
                raise ValueError("a non-executed result must not carry a trade log")

    def to_contract(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "decision_id": self.decision_id,
            "action": self.action.value,
            "status": self.status.value,
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
            "refusals": [item.to_contract() for item in self.refusals],
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


def _bounded_management_history(value: object) -> list[dict[str, Any]]:
    """The newest bounded management-history entries for the prompt.

    Keeps at most ``_MANAGEMENT_HISTORY_MAX_ENTRIES`` newest dict entries,
    truncates each free-text note/rationale, and drops the oldest entries until
    the serialized form fits ``_MANAGEMENT_HISTORY_MAX_BYTES``.
    """
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value[-_MANAGEMENT_HISTORY_MAX_ENTRIES:]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        for key in ("note", "rationale"):
            text = entry.get(key)
            if isinstance(text, str) and len(text) > _MANAGEMENT_HISTORY_TEXT_LIMIT:
                entry[key] = text[:_MANAGEMENT_HISTORY_TEXT_LIMIT]
        entries.append(entry)
    while entries and (
        len(json.dumps(entries, sort_keys=True).encode("utf-8")) > _MANAGEMENT_HISTORY_MAX_BYTES
    ):
        entries.pop(0)
    return entries


_LLM_ERROR_CODES = frozenset({"LLM_REFUSED", "LLM_OUTPUT_TRUNCATED", "LLM_SCHEMA_INVALID"})


def _llm_error_code(code: str | None) -> str:
    """Map a validator code to the error catalog's LLM vocabulary."""
    return code if code in _LLM_ERROR_CODES else "LLM_SCHEMA_INVALID"


CADENCE_CONDITION = "CADENCE"
FUNDING_SETTLEMENT_CONDITION = "FUNDING_SETTLEMENT"
RISK_BREACH_CONDITION = "RISK_BREACH"


@dataclass(frozen=True)
class ManagementDue:
    """Whether the automated cadence should ask for a management decision."""

    due: bool
    conditions: tuple[str, ...] = ()
    reason: str = ""


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def utc_day(timestamp: str) -> date | None:
    """The UTC calendar day for a recorded timestamp, or ``None``."""
    parsed = _parse_utc(timestamp)
    return None if parsed is None else parsed.astimezone(UTC).date()


def realized_net_for_day(trades: Sequence[TradeLogRecord], day: date) -> Decimal:
    """The signed net realized PnL of the trades closed on the UTC day.

    Gains and losses are both counted: a net-positive day contributes a
    positive value (and therefore no daily loss), which is what an
    account-level daily-loss measure must see.
    """
    total = Decimal("0")
    for trade in trades:
        closed = _parse_utc(trade.closed_at)
        if closed is None or closed.astimezone(UTC).date() != day:
            continue
        net = _decimal_or_none(trade.net_pnl_usd)
        if net is not None:
            total += net
    return total


def unrealized_net_for_position(
    side: str, avg_entry: Decimal, size: Decimal, mark: Decimal
) -> Decimal:
    """The position's signed unrealized PnL at its recorded mark."""
    if side == "SHORT":
        return (avg_entry - mark) * size
    if side == "LONG":
        return (mark - avg_entry) * size
    raise ValueError(f"unsupported position side {side!r}")


@dataclass(frozen=True)
class ManagementRiskContext:
    """One account-level daily-loss measurement for a management window.

    The runner builds this once per ``run_window`` and passes it to every
    per-position ``due_conditions`` call, so the trade log and open-position
    reads do not repeat once per position. ``unrealized_net is None`` means the
    measurement is unmeasured (a missing, non-finite, or stale recorded mark);
    callers must then report no breach rather than fabricate one.
    """

    day: date | None
    realized_net: Decimal
    unrealized_net: Decimal | None
    equity: Decimal

    @property
    def measured(self) -> bool:
        return self.day is not None and self.unrealized_net is not None

    @property
    def daily_loss(self) -> Decimal | None:
        """The positive net loss for the UTC day, or ``None`` when unmeasured."""
        if self.day is None or self.unrealized_net is None:
            return None
        total = self.realized_net + self.unrealized_net
        return -total if total < 0 else Decimal("0")

    @classmethod
    def unmeasured(cls) -> ManagementRiskContext:
        """A context that deliberately reports no measurement.

        The runner passes it when the account-level inputs are not needed (the
        ``RISK_BREACH`` condition is disabled or no management provider is
        configured) or when the measurement failed. An explicit unmeasured value
        tells ``due_conditions`` not to fall back to a fresh per-position read;
        ``daily_loss`` is ``None``, so no breach is reported.
        """
        return cls(
            day=None,
            realized_net=Decimal("0"),
            unrealized_net=None,
            equity=Decimal("0"),
        )


def daily_loss_breach(*, equity: Decimal, loss: Decimal, risk_pct: float) -> bool:
    """Whether the measured daily loss strictly exceeds the configured cap."""
    if equity <= 0:
        return False
    try:
        limit = equity * Decimal(str(risk_pct)) / Decimal("100")
    except (ArithmeticError, InvalidOperation, ValueError):
        return False
    return loss > limit


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
        funding_settlement_enabled: bool = True,
        risk_breach_enabled: bool = True,
    ) -> None:
        self._store = store
        self._clock = clock
        self._provider = provider
        self._settings = settings
        self._kill_switch = kill_switch
        self._funding_settlement_enabled = funding_settlement_enabled
        self._risk_breach_enabled = risk_breach_enabled
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

    # -- due/condition evaluation ---------------------------------------

    @property
    def configured(self) -> bool:
        """Whether a management LLM provider is wired in."""
        return self._provider is not None

    def due_conditions(
        self,
        position_id: str,
        *,
        window_start: str,
        window_end: str,
        now: str,
        funding_settlement_enabled: bool | None = None,
        risk_breach_enabled: bool | None = None,
        risk_context: ManagementRiskContext | None = None,
    ) -> ManagementDue:
        """Evaluate whether the cadence should ask for a decision now.

        The runner calls this only at a window boundary, so ``CADENCE`` is the
        baseline condition for every readable OPEN position. ``FUNDING_SETTLEMENT``
        and ``RISK_BREACH`` are additive conditions gated by their toggles. The
        runner passes the window's frozen account-level ``risk_context`` so the
        daily-loss inputs are read once for all positions; a direct caller that
        omits it gets a fresh measurement. This method never calls the LLM,
        executes anything, or writes an order.
        """
        record = self._store.get_position(position_id)
        if record is None:
            return ManagementDue(False, (), f"position {position_id} does not exist")
        try:
            payload = json.loads(record.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return ManagementDue(False, (), f"the position {position_id} payload is unreadable")
        if not isinstance(payload, dict):
            return ManagementDue(False, (), f"the position {position_id} payload is unreadable")
        state = payload.get("state")
        if state != "OPEN":
            return ManagementDue(False, (), f"position {position_id} is {state}, not OPEN")
        try:
            position_from_contract(payload)
        except PositionError as exc:
            return ManagementDue(False, (), exc.message)

        conditions: list[str] = [CADENCE_CONDITION]
        funding_enabled = (
            self._funding_settlement_enabled
            if funding_settlement_enabled is None
            else funding_settlement_enabled
        )
        if funding_enabled and self._positions.funding_settlement_in_window(
            position_id, window_start, window_end
        ):
            conditions.append(FUNDING_SETTLEMENT_CONDITION)
        risk_enabled = (
            self._risk_breach_enabled if risk_breach_enabled is None else risk_breach_enabled
        )
        if risk_enabled:
            measured = risk_context if risk_context is not None else self.risk_context(now=now)
            if self._risk_breach(measured):
                conditions.append(RISK_BREACH_CONDITION)
        return ManagementDue(True, tuple(conditions), "due: " + ", ".join(conditions))

    def risk_context(
        self,
        *,
        now: str,
        open_records: Sequence[PositionRecord] | None = None,
    ) -> ManagementRiskContext:
        """Measure the account-level daily loss once for a cadence window.

        The loss is the signed net realized PnL for the UTC day plus the signed
        unrealized PnL of every OPEN position at its recorded mark. If any open
        position's mark is missing, non-finite, unreadable, or older than
        ``risk_stale_mark_seconds`` the measurement is unmeasured (``None``):
        the caller must then report no breach. No mark is fabricated.
        """
        day = utc_day(now)
        realized = (
            realized_net_for_day(self._store.list_trade_logs(limit=None), day)
            if day is not None
            else Decimal("0")
        )
        records = (
            open_records
            if open_records is not None
            else self._store.list_positions(state="OPEN", limit=None)
        )
        account = self._paper.ensure()
        cash = _decimal_or_none(account.balance)
        if cash is None:
            return ManagementRiskContext(day, realized, None, Decimal("0"))
        payloads: list[dict[str, Any]] = []
        try:
            for record in records:
                payload = json.loads(record.payload_json)
                if not isinstance(payload, dict):
                    raise ValueError("a position payload must be a JSON object")
                payloads.append(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return ManagementRiskContext(day, realized, None, cash)
        equity, view = compute_equity(cash, payloads)
        if view.stale_inputs:
            return ManagementRiskContext(day, realized, None, equity)
        now_instant = self._clock.now()
        stale_seconds = int(self._settings.risk_stale_mark_seconds)
        unrealized = Decimal("0")
        for payload in payloads:
            mark = _decimal_or_none(payload.get("mark_price"))
            size = _decimal_or_none(payload.get("size"))
            avg_entry = _decimal_or_none(payload.get("avg_entry_price"))
            mark_as_of = _parse_utc(payload.get("mark_as_of"))
            side = str(payload.get("side", "LONG"))
            if mark is None or size is None or avg_entry is None or mark_as_of is None:
                return ManagementRiskContext(day, realized, None, equity)
            if (now_instant - mark_as_of).total_seconds() > stale_seconds:
                return ManagementRiskContext(day, realized, None, equity)
            try:
                unrealized += unrealized_net_for_position(side, avg_entry, size, mark)
            except ValueError:
                return ManagementRiskContext(day, realized, None, equity)
        return ManagementRiskContext(day, realized, unrealized, equity)

    def _risk_breach(self, context: ManagementRiskContext) -> bool:
        loss = context.daily_loss
        if loss is None:
            return False
        return daily_loss_breach(
            equity=context.equity, loss=loss, risk_pct=self._settings.risk_daily_loss_pct
        )

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
        # The MANAGEMENT call (plus a possible repair) can take tens of
        # seconds; anchor every derived timestamp -- provenance, envelope,
        # verdict expiry, gate inputs, and audits -- to a clock read AFTER it
        # returns, so a slow valid response cannot persist an already-expired
        # verdict (H1).
        context = replace(context, now=utc_timestamp(self._clock.now()))
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

        return self._dispatch_authorized(
            context,
            decision_id=decision_id,
            action=action,
            management=management,
            llm_view=llm_view,
        )

    # -- classification and dispatch -------------------------------------

    def _dispatch_authorized(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        action: str,
        management: dict[str, Any],
        llm_view: dict[str, Any],
        idempotent_replay: bool = False,
    ) -> ManagementResult:
        """Classify, apply the kill-switch policy, execute, and record the outcome.

        Every request that reaches this point appends a ``MANAGEMENT_OUTCOME``
        audit event: ``EXECUTED``/``BLOCKED``/``NO_ACTION`` are terminal, while a
        ``REFUSED`` outcome (kill switch, missing contract/caps, gate or
        validation error) is not, so a same-key replay may re-attempt it. This
        is the one post-validation dispatch path shared by the live call and the
        replay re-attempt.
        """
        position = context.position
        try:
            classification = classify_management_intent(
                action=action,
                position_side=position.side,
                current_stop=position.stop_price,
                new_stop_price=json_decimal(management.get("new_stop_price")),
            )
            self._audit_classification(context, decision_id, action, classification)
            if classification.risk_increasing:
                self._kill_switch.require_clear()

            if action == ManagementAction.HOLD.value:
                position_payload = self._positions.record_management_hold(
                    position.position_id,
                    decision_id=decision_id,
                    note=_note(management),
                    now=context.now,
                )
                result = self._result(
                    context,
                    decision_id=decision_id,
                    action=action,
                    status=ManagementStatus.NO_ACTION,
                    executed=False,
                    reason_code="HOLD",
                    reason="the management decision was HOLD; no order was created",
                    llm=llm_view,
                    position=position_payload,
                )
            elif action == ManagementAction.FULL_CLOSE.value:
                result = self._full_close(
                    context, decision_id=decision_id, llm=llm_view, reason=None
                )
            elif action == ManagementAction.PARTIAL_CLOSE.value:
                result = self._partial_close(
                    context, decision_id=decision_id, management=management, llm=llm_view
                )
            elif action == ManagementAction.ADD.value:
                result = self._add(
                    context, decision_id=decision_id, management=management, llm=llm_view
                )
            else:
                # The action vocabulary is closed and already validated above; a
                # value that reaches here is a single-repair bug, never an
                # implicit ADD.
                raise ManagementRejected(
                    "LLM_SCHEMA_INVALID", f"unsupported management action {action!r}"
                )
        except KillSwitchActive as exc:
            self._record_outcome(
                position_id=position.position_id,
                at=context.now,
                decision_id=decision_id,
                action=action,
                outcome="REFUSED",
                reason_code=exc.code,
                message=exc.message,
                terminal=False,
            )
            raise ManagementRejected(exc.code, exc.message) from exc
        except (ManagementRejected, PositionCloseError, PositionNotFound) as exc:
            self._record_outcome(
                position_id=position.position_id,
                at=context.now,
                decision_id=decision_id,
                action=action,
                outcome="REFUSED",
                reason_code=exc.code,
                message=exc.message,
                terminal=False,
            )
            raise
        self._record_outcome_from_result(
            position_id=position.position_id,
            at=context.now,
            decision_id=decision_id,
            result=result,
        )
        return replace(result, idempotent_replay=True) if idempotent_replay else result

    def _record_outcome(
        self,
        *,
        position_id: str,
        at: str,
        decision_id: str,
        action: str,
        outcome: str,
        reason_code: str,
        message: str | None,
        terminal: bool,
        risk: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "decision_id": decision_id,
            "action": action,
            "outcome": outcome,
            "reason_code": reason_code,
            "message": message,
            "terminal": terminal,
        }
        if risk is not None:
            payload["risk"] = risk
        record_audit(
            self._store,
            event_type="MANAGEMENT_OUTCOME",
            actor={"kind": "SYSTEM", "id": "management_service"},
            subject={"kind": "POSITION", "id": position_id},
            payload=payload,
            at=at,
        )

    def _record_outcome_from_result(
        self,
        *,
        position_id: str,
        at: str,
        decision_id: str,
        result: ManagementResult,
    ) -> None:
        if result.status is ManagementStatus.EXECUTED:
            outcome = "EXECUTED"
            terminal = True
        elif result.status is ManagementStatus.BLOCKED:
            outcome = "BLOCKED"
            terminal = True
        else:
            outcome = "NO_ACTION"
            terminal = True
        self._record_outcome(
            position_id=position_id,
            at=at,
            decision_id=decision_id,
            action=result.action.value,
            outcome=outcome,
            reason_code=result.reason_code,
            message=result.reason,
            terminal=terminal,
            risk=result.risk,
        )

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
        refusals = self._apply_stop_targets(
            context, decision_id=decision_id, action=purpose, management=management
        )
        position_payload = self._read_position_payload(position.position_id)
        return self._result(
            context,
            decision_id=decision_id,
            action=purpose,
            status="EXECUTED",
            executed=True,
            reason_code="PARTIAL_CLOSE",
            reason=_executed_reason(
                "the position was partially reduced by the management decision", refusals
            ),
            llm=llm,
            risk=self._risk_view(risk_verdict_id),
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
            refusals=tuple(refusals),
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
        refusals = self._apply_stop_targets(
            context,
            decision_id=decision_id,
            action=ManagementAction.ADD.value,
            management=management,
        )
        position_payload = self._read_position_payload(position.position_id)
        clamped = requested_quantity is not None and requested_quantity != allowed_quantity
        return self._result(
            context,
            decision_id=decision_id,
            action=ManagementAction.ADD.value,
            status="EXECUTED",
            executed=True,
            reason_code="ADD",
            reason=_executed_reason(
                f"the add was clamped to the stricter cap ({execution.binding_cap.value})"
                if clamped
                else "the add was executed within every cap",
                refusals,
            ),
            llm=llm,
            risk=execution.to_view(),
            order=result.order.to_contract(),
            fill=result.fill.to_contract(),
            position=position_payload,
            refusals=tuple(refusals),
        )

    def _apply_stop_targets(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        action: str,
        management: dict[str, Any],
    ) -> list[ManagementRefusal]:
        """Apply the validated stop/target changes, refusing any stop loosening.

        The manual management path forbids every stop loosening/removal. A
        loosened stop is never applied, but it does not discard the companion
        reduce-only close (already executed) or valid target changes. The
        refusal is audited and returned so the caller can surface it.
        """
        new_stop = json_decimal(management.get("new_stop_price"))
        raw_targets = management.get("new_targets") or []
        targets = (
            tuple(Decimal(str(target)) for target in raw_targets)
            if isinstance(raw_targets, list) and raw_targets
            else None
        )
        if new_stop is None and targets is None:
            return []
        refusals: list[ManagementRefusal] = []
        if new_stop is not None:
            refusal = self._stop_refusal(
                side=context.position.side,
                current_stop=context.position.stop_price,
                new_stop=new_stop,
            )
            if refusal is not None:
                refusals.append(refusal)
                self._audit_stop_refused(
                    context,
                    decision_id=decision_id,
                    action=action,
                    refusal=refusal,
                )
                new_stop = None
        if new_stop is None and targets is None:
            return refusals
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
        return refusals

    @staticmethod
    def _stop_refusal(
        *,
        side: PositionSide,
        current_stop: Decimal | None,
        new_stop: Decimal,
    ) -> ManagementRefusal | None:
        """A typed refusal when the proposed stop loosens/removes the current one."""
        if current_stop is None:
            return None
        change = classify_management_intent(
            action=ManagementAction.PARTIAL_CLOSE.value,
            position_side=side,
            current_stop=current_stop,
            new_stop_price=new_stop,
        ).stop_change
        if change not in (StopChange.LOOSEN, StopChange.REMOVE):
            return None
        return ManagementRefusal(
            code="RISK_BLOCKED",
            field=_STOP_REFUSAL_FIELD,
            message="a management action may not loosen or remove the protective stop",
        )

    def _audit_stop_refused(
        self,
        context: ManagementMark,
        *,
        decision_id: str,
        action: str,
        refusal: ManagementRefusal,
    ) -> None:
        record_audit(
            self._store,
            event_type="MANAGEMENT_STOP_REFUSED",
            actor={"kind": "SYSTEM", "id": "management_service"},
            subject={"kind": "POSITION", "id": context.position.position_id},
            payload={
                "decision_id": decision_id,
                "action": action,
                "code": refusal.code,
                "field": refusal.field,
                "message": refusal.message,
            },
            at=context.now,
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
            "management_history": _bounded_management_history(history),
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
                payload_json=json.dumps(envelope, sort_keys=True, allow_nan=False),
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
        refusals: tuple[ManagementRefusal, ...] = (),
        idempotent_replay: bool = False,
    ) -> ManagementResult:
        account = self._paper.summary()
        return ManagementResult(
            position_id=context.position.position_id,
            decision_id=decision_id,
            action=ManagementAction(action),
            status=ManagementStatus(status),
            executed=executed,
            reason_code=reason_code,
            reason=reason,
            llm=llm,
            risk=risk,
            order=order,
            fill=fill,
            position=position,
            trade_log=trade_log,
            refusals=refusals,
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
        """Return the stored outcome, or re-attempt an unexecuted valid decision.

        Replay fails closed: a stored payload that cannot be parsed into the
        closed action vocabulary, or an executed action missing its order/fill
        (or, for a full close, its matching trade log), is a typed
        ``DATA_UNAVAILABLE``. A decision whose execution was refused for a
        transient reason is re-attempted through the same post-validation
        dispatch path, without re-running the LLM, re-saving the decision, or
        creating a second order.
        """
        record = self._store.get_decision(decision_id)
        if record is None:
            return None
        payload = self._decode_stored(
            record.payload_json, "the stored management decision is unreadable"
        )
        provenance = payload.get("provenance")
        recorded_provider = provenance.get("provider") if isinstance(provenance, dict) else None
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
        action, management = self._stored_management(payload)
        position_record = self._store.get_position(position_id)
        if position_record is None:
            raise ManagementRejected("POSITION_NOT_FOUND", f"position {position_id} does not exist")
        position_payload = self._decode_stored(
            position_record.payload_json, f"the stored position {position_id} is unreadable"
        )
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
            "action": action.value,
            "validation_status": record.validation_status,
            "confidence": payload.get("confidence"),
            "rationale": payload.get("rationale"),
            "provenance": provenance,
        }
        order, purpose = self._replay_order(decision_id, action, entry)
        if order is not None:
            return self._replayed_executed(
                position_id=position_id,
                decision_id=decision_id,
                action=action.value,
                purpose=purpose or action.value,
                order=order,
                position_payload=position_payload,
                management=management,
                llm_view=llm_view,
            )
        outcome = self._last_outcome(decision_id)
        if outcome is not None and outcome.get("terminal") and outcome.get("outcome") == "BLOCKED":
            risk = outcome.get("risk")
            return ManagementResult(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                status=ManagementStatus.BLOCKED,
                executed=False,
                reason_code=str(outcome.get("reason_code") or "RISK_BLOCKED"),
                reason=str(
                    outcome.get("message")
                    or "the original management action was blocked; no order was created"
                ),
                llm=llm_view,
                risk=risk if isinstance(risk, dict) else None,
                position=position_payload,
                account=self._account_view(),
                idempotent_replay=True,
            )
        if outcome is not None and outcome.get("terminal") and outcome.get("outcome") == "EXECUTED":
            # The stored result says an order committed but no deterministic-key
            # or history-referenced order exists: never re-execute a possibly
            # committed action, and never report EXECUTED without its evidence.
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                "the recorded management execution has no order or fill",
            )
        if action is ManagementAction.HOLD and entry is not None:
            return ManagementResult(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                status=ManagementStatus.NO_ACTION,
                executed=False,
                reason_code="HOLD",
                reason="the original management decision was HOLD; no order was created",
                llm=llm_view,
                position=position_payload,
                account=self._account_view(),
                idempotent_replay=True,
            )
        return self._reattempt_replay(
            position_id=position_id,
            decision_id=decision_id,
            action=action,
            management=management,
            llm_view=llm_view,
        )

    @staticmethod
    def _decode_stored(raw: str, message: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ManagementRejected("DATA_UNAVAILABLE", message) from exc
        if not isinstance(payload, dict):
            raise ManagementRejected("DATA_UNAVAILABLE", message)
        return payload

    @staticmethod
    def _stored_management(payload: dict[str, Any]) -> tuple[ManagementAction, dict[str, Any]]:
        management = payload.get("position_management")
        if not isinstance(management, dict):
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                "the stored management decision has no position_management payload",
            )
        raw_action = management.get("action")
        try:
            action = ManagementAction(str(raw_action))
        except ValueError as exc:
            raise ManagementRejected(
                "DATA_UNAVAILABLE",
                f"the stored management decision has an unknown action {raw_action!r}",
            ) from exc
        return action, management

    def _replay_order(
        self,
        decision_id: str,
        action: ManagementAction,
        entry: dict[str, Any] | None,
    ) -> tuple[OrderRecord | None, str | None]:
        """The committed order for this decision, by history row or deterministic key."""
        if entry is not None and entry.get("executed_order_id") is not None:
            order_id = str(entry["executed_order_id"])
            order = self._store.get_order(order_id)
            if order is None:
                raise ManagementRejected(
                    "DATA_UNAVAILABLE", f"the replayed order {order_id} is missing"
                )
            return order, order.purpose or action.value
        purposes = [action.value]
        if action is ManagementAction.PARTIAL_CLOSE:
            # A slice that rounds to zero or leaves a sub-minimum remainder
            # escalates to a full close under the FULL_CLOSE order key.
            purposes.append(ManagementAction.FULL_CLOSE.value)
        for purpose in purposes:
            order = self._store.get_order_by_idempotency(_order_key(decision_id, purpose))
            if order is not None:
                return order, purpose
        return None, None

    def _replayed_executed(
        self,
        *,
        position_id: str,
        decision_id: str,
        action: str,
        purpose: str,
        order: OrderRecord,
        position_payload: dict[str, Any],
        management: dict[str, Any],
        llm_view: dict[str, Any],
    ) -> ManagementResult:
        """Reconstruct an executed result only from complete, matching evidence.

        A stop/target change whose second transaction was lost is reconciled
        only while its pre-change snapshot still matches; when that cannot be
        verified the replay raises a typed ``DATA_UNAVAILABLE`` instead of
        reporting ``EXECUTED`` with the change silently dropped (see
        :meth:`_reconcile_replayed_change`).
        """
        if order.decision_id is not None and order.decision_id != decision_id:
            raise ManagementRejected(
                "DATA_UNAVAILABLE", "the replayed order belongs to a different decision"
            )
        order_payload = self._decode_stored(
            order.payload_json, f"the replayed order {order.order_id} is unreadable"
        )
        fill = self._store.get_fill_by_order_id(order.order_id)
        if fill is None:
            raise ManagementRejected(
                "DATA_UNAVAILABLE", f"the replayed fill for order {order.order_id} is missing"
            )
        fill_payload = self._decode_stored(
            fill.payload_json, f"the replayed fill {fill.fill_id} is unreadable"
        )
        trade_payload: dict[str, Any] | None = None
        if purpose == ManagementAction.FULL_CLOSE.value:
            trade = self._store.get_trade_log_for_position(position_id)
            if trade is None or trade.decision_id != decision_id:
                raise ManagementRejected(
                    "DATA_UNAVAILABLE",
                    "the replayed full close has no trade log for this decision",
                )
            trade_payload = self._decode_stored(
                trade.payload_json, f"the replayed trade log {trade.trade_id} is unreadable"
            )
        refusals: list[ManagementRefusal] = []
        if position_payload.get("state") == "OPEN":
            # A stop/target update is a second transaction after the fill. If it
            # was lost (a crash between the two commits), a same-key replay must
            # apply it idempotently instead of reporting success with the stale
            # stop still in place.
            position_payload, refusals = self._reconcile_replayed_change(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                management=management,
                position_payload=position_payload,
            )
        reason = _executed_reason("the original management result was replayed", refusals)
        return ManagementResult(
            position_id=position_id,
            decision_id=decision_id,
            action=ManagementAction(action),
            status=ManagementStatus.EXECUTED,
            executed=True,
            reason_code=action,
            reason=reason,
            llm=llm_view,
            risk=self._risk_view(order.risk_verdict_id),
            order=order_payload,
            fill=fill_payload,
            position=position_payload,
            trade_log=trade_payload,
            account=self._account_view(),
            refusals=tuple(refusals),
            idempotent_replay=True,
        )

    def _reattempt_replay(
        self,
        *,
        position_id: str,
        decision_id: str,
        action: ManagementAction,
        management: dict[str, Any],
        llm_view: dict[str, Any],
    ) -> ManagementResult:
        """Re-execute a stored VALID/REPAIRED decision that never executed.

        The LLM is not called and the decision is not re-saved; the fresh
        recorded mark and clock make the deterministic order key the only write,
        and a pre-existing order (including an ``ORDER_DUPLICATE`` race) is
        reconstructed rather than duplicated.
        """
        try:
            context = self._positions.management_mark(position_id)
        except (PositionCloseError, PositionNotFound) as exc:
            self._record_outcome(
                position_id=position_id,
                at=utc_timestamp(self._clock.now()),
                decision_id=decision_id,
                action=action.value,
                outcome="REFUSED",
                reason_code=exc.code,
                message=exc.message,
                terminal=False,
            )
            raise
        context = replace(context, now=utc_timestamp(self._clock.now()))
        try:
            return self._dispatch_authorized(
                context,
                decision_id=decision_id,
                action=action.value,
                management=management,
                llm_view=llm_view,
                idempotent_replay=True,
            )
        except (ManagementRejected, PositionCloseError) as exc:
            if exc.code != "ORDER_DUPLICATE":
                raise
            order, purpose = self._replay_order(decision_id, action, None)
            if order is None:
                raise
            position_record = self._store.get_position(position_id)
            if position_record is None:
                raise ManagementRejected(
                    "POSITION_NOT_FOUND", f"position {position_id} does not exist"
                ) from exc
            position_payload = self._decode_stored(
                position_record.payload_json, f"the stored position {position_id} is unreadable"
            )
            return self._replayed_executed(
                position_id=position_id,
                decision_id=decision_id,
                action=action.value,
                purpose=purpose or action.value,
                order=order,
                position_payload=position_payload,
                management=management,
                llm_view=llm_view,
            )

    def _last_outcome(self, decision_id: str) -> dict[str, Any] | None:
        """The most recent recorded outcome for this decision, if any."""
        for event in reversed(self._store.list_audit_events_ascending(limit=None)):
            if event.event_type != "MANAGEMENT_OUTCOME":
                continue
            try:
                body = json.loads(event.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            payload = body.get("payload") if isinstance(body, dict) else None
            if isinstance(payload, dict) and payload.get("decision_id") == decision_id:
                return payload
        return None

    def _reconcile_replayed_change(
        self,
        *,
        position_id: str,
        decision_id: str,
        action: str,
        management: dict[str, Any],
        position_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[ManagementRefusal]]:
        """Idempotently apply a stored stop/target change missing from the row.

        A stop/target update is a second transaction after the fill. If it was
        lost (a crash between the two commits), a same-key replay must apply it
        exactly once. Detection is evidence-based, not assumed:

        * When this decision's ``STOP_UPDATED`` audit exists the change was
          applied; the replay returns ``EXECUTED`` untouched, preserving the
          stale-key no-rollback behavior.
        * When it does not exist and the decision carries a change, the write is
          reconciled only while the row still equals the pre-change
          ``MANAGEMENT_CLASSIFIED`` snapshot. Later ``HOLD`` entries change no
          stop/targets and are ignored; a later non-``HOLD`` management action,
          or ambiguous ordering, blocks reconciliation.
        * When any guard cannot be established (missing snapshot, persisted
          values that differ from the snapshot without an applied audit,
          ambiguous ordering) the replay fails closed: it writes a
          ``MANAGEMENT_RECONCILE_SKIPPED`` audit and raises a typed
          ``DATA_UNAVAILABLE``. It never reports ``EXECUTED`` while silently
          dropping the change.

        A loosened/removed stop is never re-applied; its typed refusal is
        surfaced instead.
        """
        if action not in {
            ManagementAction.PARTIAL_CLOSE.value,
            ManagementAction.ADD.value,
        }:
            return position_payload, []
        new_stop = json_decimal(management.get("new_stop_price"))
        raw_targets = management.get("new_targets") or []
        targets = (
            tuple(Decimal(str(target)) for target in raw_targets)
            if isinstance(raw_targets, list) and raw_targets
            else None
        )
        if new_stop is None and targets is None:
            return position_payload, []
        if self._management_change_applied(decision_id):
            # The stop/target transaction committed, so its change is already in
            # the row (or was superseded by a newer one). Never rewrite it.
            return position_payload, []
        history = position_payload.get("management_history")
        reason = self._unordered_change_reason(history, decision_id)
        if reason is not None:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason=reason,
            )
        snapshot = self._management_snapshot(decision_id)
        if snapshot is None:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the pre-change management snapshot is missing",
            )
        persisted_stop = json_decimal(position_payload.get("stop_price"))
        recorded_stop = json_decimal(snapshot.get("stop_price"))
        if persisted_stop != recorded_stop:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the stored stop no longer matches the pre-change snapshot",
            )
        persisted_targets = [
            json_decimal(target) for target in (position_payload.get("target_prices") or [])
        ]
        recorded_targets = [
            json_decimal(target) for target in (snapshot.get("target_prices") or [])
        ]
        if persisted_targets != recorded_targets:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the stored targets no longer match the pre-change snapshot",
            )
        try:
            side = PositionSide(str(position_payload.get("side") or ""))
        except ValueError:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the stored position side is invalid",
            )
        refusals: list[ManagementRefusal] = []
        try:
            classification = classify_management_intent(
                action=action,
                position_side=side,
                current_stop=recorded_stop,
                new_stop_price=new_stop,
            )
        except ValueError:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the recovered stop change cannot be classified",
            )
        if new_stop is not None:
            refusal = self._stop_refusal(
                side=side,
                current_stop=persisted_stop,
                new_stop=new_stop,
            )
            if refusal is not None:
                refusals.append(refusal)
                new_stop = None
        stop_applied = new_stop is None or persisted_stop == new_stop
        targets_applied = targets is None or list(targets) == persisted_targets
        if stop_applied and targets_applied:
            return position_payload, refusals
        decision_mark = json_decimal(snapshot.get("mark_price"))
        if new_stop is not None and decision_mark is None:
            self._skip_reconcile(
                position_id=position_id,
                decision_id=decision_id,
                action=action,
                reason="the pre-change mark is missing",
            )
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
        updated = self._positions.apply_management_stop(
            position_id,
            stop_price=new_stop,
            target_prices=targets,
            decision_id=decision_id,
            now=now,
            reference=decision_mark,
        )
        return updated, refusals

    def _management_change_applied(self, decision_id: str) -> bool:
        """Whether this decision's stop/target transaction committed.

        The ``STOP_UPDATED`` audit is written atomically with the row change, so
        its presence (with this decision's id in the payload) is the evidence
        that the change was applied. Its absence is the only state in which a
        replay may reconcile.
        """
        for event in reversed(self._store.list_audit_events_ascending(limit=None)):
            if event.event_type != "STOP_UPDATED":
                continue
            try:
                body = json.loads(event.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            payload = body.get("payload") if isinstance(body, dict) else None
            if isinstance(payload, dict) and payload.get("decision_id") == decision_id:
                return True
        return False

    @staticmethod
    def _unordered_change_reason(history: object, decision_id: str) -> str | None:
        """Why ordering blocks reconciliation, or ``None`` when it is safe.

        The decision's own history entry must exist: the order, fill, and
        history row commit atomically, so a missing entry means the timeline
        cannot be trusted. Later ``HOLD`` entries change no stop/targets and are
        ignored; any later non-``HOLD`` management action owns the row, and an
        unreadable later entry makes the ordering ambiguous. Both fail closed.
        """
        if not isinstance(history, list):
            return "the recorded management history is unreadable"
        index: int | None = None
        for position, entry in enumerate(history):
            if isinstance(entry, dict) and entry.get("decision_id") == decision_id:
                index = position
        if index is None:
            return "the recorded management history has no entry for this decision"
        for entry in history[index + 1 :]:
            if not isinstance(entry, dict):
                return "the recorded management history has an unreadable later entry"
            if entry.get("action") != ManagementAction.HOLD.value:
                return "a later management action owns this position"
        return None

    def _skip_reconcile(
        self,
        *,
        position_id: str,
        decision_id: str,
        action: str,
        reason: str,
    ) -> NoReturn:
        """Audit and fail closed when a lost change cannot be verified."""
        record_audit(
            self._store,
            event_type="MANAGEMENT_RECONCILE_SKIPPED",
            actor={"kind": "SYSTEM", "id": "management_service"},
            subject={"kind": "POSITION", "id": position_id},
            payload={
                "decision_id": decision_id,
                "action": action,
                "reason": reason,
            },
            at=utc_timestamp(self._clock.now()),
        )
        raise ManagementRejected(
            "DATA_UNAVAILABLE",
            f"the replayed management change cannot be verified: {reason}",
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


def _executed_reason(base: str, refusals: Sequence[ManagementRefusal]) -> str:
    """Append the typed refusal code(s) to an executed result's reason text."""
    if not refusals:
        return base
    codes = ", ".join(item.code for item in refusals)
    return f"{base}; the stop change was refused ({codes})"


def _note(management: dict[str, Any]) -> str:
    rationale = str(management.get("rationale") or "").strip()
    urgency = str(management.get("urgency") or "")
    base = f"management {management.get('action', '').lower()}"
    if urgency:
        base += f" ({urgency.lower()} urgency)"
    if rationale:
        base += f": {rationale[:200]}"
    return base


__all__ = [
    "CADENCE_CONDITION",
    "FUNDING_SETTLEMENT_CONDITION",
    "ManagementDue",
    "ManagementRefusal",
    "ManagementRejected",
    "ManagementResult",
    "ManagementRiskContext",
    "ManagementService",
    "RISK_BREACH_CONDITION",
    "daily_loss_breach",
    "realized_net_for_day",
    "unrealized_net_for_position",
    "utc_day",
]
