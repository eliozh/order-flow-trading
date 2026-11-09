"""Provider orchestration: role/model resolution, retries, repair, breaker (D5).

The application layer owns the policy; adapters only translate transport. One
repair follows a schema failure, transport/5xx/429/timeout failures retry once,
refusals and truncations are never retried, and a final rejection is recorded
locally as an ``LLM_REJECTED`` audit event (never Telegram).
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, cast

import structlog

from oft.application.budget_ledger import BudgetEntry, BudgetLedger, stable_call_id
from oft.application.llm_validation import (
    ValidationIssue,
    ValidationOutcome,
    hash_text,
    validate_output,
)
from oft.config import Settings
from oft.domain.prompts import rendered_prompt_hash
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import (
    CircuitOpenError,
    LLMAdapterError,
    LLMRequest,
    LLMResult,
    LLMRole,
    LLMSchemaUnsupported,
    LLMTimeoutError,
    LLMTransportError,
    LLMUsage,
    ProbeOutcome,
    redact,
    sanitize_provider_id,
)
from oft.ports.store import (
    AuditEventDraft,
    LLMCallRecord,
    ProviderCapabilityRecord,
    StorePort,
)

ANALYSIS_TIMEOUT_SECONDS = 120.0
ENTRY_RECHECK_TIMEOUT_SECONDS = 45.0
_RETRY_BASE_SECONDS = 0.5
_RETRY_ATTEMPTS = 2
_MAX_JITTER_SECONDS = 0.25

LOGGER = structlog.get_logger(__name__)
_REAL_PROVIDER_KEYS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
}

AdapterFactory = Callable[[Settings, str], Any]
_default_adapter_factory: AdapterFactory | None = None


def set_default_adapter_factory(factory: AdapterFactory) -> None:
    """Register the composition root's provider-construction seam.

    The application layer must not import ``oft.bootstrap`` (the execution path
    is not allowed to reach adapters), so the root injects this seam instead.
    """
    global _default_adapter_factory
    _default_adapter_factory = factory


def _registered_adapter_factory() -> AdapterFactory:
    if _default_adapter_factory is None:
        raise LLMConfigurationError(
            "LLM_PROVIDER_NOT_CONFIGURED",
            "no provider-construction seam is registered; build the container first",
        )
    return _default_adapter_factory


def _secret_value(secret: object) -> str | None:
    if secret is None:
        return None
    getter = getattr(secret, "get_secret_value", None)
    value = getter() if callable(getter) else str(secret)
    return value if isinstance(value, str) and value.strip() else None


def provider_api_key(settings: Settings, name: str) -> str | None:
    """Return a provider key from settings; the value never enters a log."""
    attribute = _REAL_PROVIDER_KEYS.get(name.strip().lower())
    if attribute is None:
        return None
    return _secret_value(getattr(settings, attribute, None))


def provider_configured(settings: Settings, name: str) -> bool:
    if name.strip().lower() == "stub":
        return True
    return provider_api_key(settings, name) is not None


def role_models(settings: Settings) -> dict[str, str | None]:
    return dict(settings.model_for_role_map())


def timeout_for_role(role: LLMRole) -> float:
    return (
        ENTRY_RECHECK_TIMEOUT_SECONDS
        if role is LLMRole.ENTRY_RECHECK
        else (ANALYSIS_TIMEOUT_SECONDS)
    )


class LLMConfigurationError(Exception):
    """A provider/role/model is not configured; carries a catalog code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _BreakerState:
    consecutive: int = 0
    open_until: datetime | None = None
    half_open: bool = False
    probe_in_flight: bool = False
    # Incremented for every admitted half-open probe so a permit can prove it
    # still owns the probe when it abandons (N1).
    probe_generation: int = 0


class CircuitBreaker:
    """Three consecutive 5xx/429 failures open a provider for 60 seconds."""

    def __init__(
        self,
        *,
        clock: ClockPort,
        threshold: int = 3,
        open_seconds: float = 60.0,
    ) -> None:
        self._clock = clock
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._states: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def _state(self, provider: str) -> _BreakerState:
        return self._states.setdefault(provider, _BreakerState())

    def before_call(self, provider: str) -> CircuitPermit:
        with self._lock:
            state = self._state(provider)
            now = self._clock.now()
            if state.open_until is not None:
                if now < state.open_until:
                    raise CircuitOpenError("circuit open")
                # The cooldown elapsed: allow exactly one half-open probe.
                state.open_until = None
                state.half_open = True
                state.probe_in_flight = False
            probe_token: int | None = None
            if state.half_open:
                if state.probe_in_flight:
                    raise CircuitOpenError("circuit open")
                state.probe_in_flight = True
                state.probe_generation += 1
                probe_token = state.probe_generation
        return CircuitPermit(self, provider, probe_token)

    def record_success(self, provider: str) -> None:
        with self._lock:
            state = self._state(provider)
            state.consecutive = 0
            state.open_until = None
            state.half_open = False
            state.probe_in_flight = False

    def record_failure(self, provider: str, *, auth: bool = False) -> None:
        with self._lock:
            state = self._state(provider)
            # Any recorded outcome resolves a half-open probe, including an auth
            # failure that does not count toward the transport threshold (F1).
            state.probe_in_flight = False
            if auth:
                return
            state.consecutive += 1
            if state.half_open or state.consecutive >= self.threshold:
                state.open_until = self._clock.now() + timedelta(seconds=self.open_seconds)
                state.consecutive = 0
                state.half_open = False
                state.probe_in_flight = False

    def release_probe(self, provider: str, probe_token: int | None = None) -> None:
        """Release a half-open probe that exited without a verdict (F1).

        The circuit stays half-open so the next caller may probe again instead
        of every later call failing with a permanent 429. Release is scoped to
        the generation the permit was admitted under: a stale permit from before
        the circuit opened must not clear a newer probe (N1).
        """
        with self._lock:
            state = self._state(provider)
            if probe_token is not None and state.probe_generation == probe_token:
                state.probe_in_flight = False

    def consecutive_failures(self, provider: str) -> int:
        with self._lock:
            return self._state(provider).consecutive

    def is_open(self, provider: str) -> bool:
        with self._lock:
            state = self._state(provider)
            return state.open_until is not None and self._clock.now() < state.open_until


@dataclass
class CircuitPermit:
    """A scoped call permit that must resolve (or abandon) a breaker probe (F1)."""

    breaker: CircuitBreaker
    provider: str
    probe_token: int | None = None
    resolved: bool = False

    def success(self) -> None:
        if self.resolved:
            return
        self.resolved = True
        self.breaker.record_success(self.provider)

    def failure(self) -> None:
        if self.resolved:
            return
        self.resolved = True
        self.breaker.record_failure(self.provider)

    def abandon(self) -> None:
        if self.resolved:
            return
        self.resolved = True
        self.breaker.release_probe(self.provider, self.probe_token)


class LLMRuntimeRegistry:
    """One process-lifetime breaker and per-provider semaphore per provider (H1).

    The composition root builds exactly one registry and injects it into every
    orchestrator and the capability probe, so the 60 s circuit and the
    concurrency caps actually apply across scans and API requests.
    """

    def __init__(self, *, clock: ClockPort, settings: Settings | None = None) -> None:
        self._clock = clock
        self._settings = settings
        self.breaker = CircuitBreaker(clock=clock)
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def semaphore(self, provider: str) -> threading.BoundedSemaphore:
        with self._lock:
            existing = self._semaphores.get(provider)
            if existing is not None:
                return existing
            limit = 1 if provider == "deepseek" else 2
            if self._settings is not None:
                limit = (
                    self._settings.llm_max_concurrency_deepseek
                    if provider == "deepseek"
                    else self._settings.llm_max_concurrency
                )
            created = threading.BoundedSemaphore(limit)
            self._semaphores[provider] = created
            return created


@dataclass(frozen=True)
class OrchestrationResult:
    outcome: ValidationOutcome
    calls: tuple[LLMCallRecord, ...]
    budgets: tuple[BudgetEntry, ...]
    first_result: LLMResult
    final_result: LLMResult


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _default_jitter() -> float:
    return random.uniform(0.0, _MAX_JITTER_SECONDS)


def _adapter_max_tokens(adapter: Any) -> int:
    capabilities = getattr(adapter, "capabilities", None)
    if callable(capabilities):
        return int(getattr(capabilities(), "max_output_tokens", 4096))
    return 4096


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Scrub string values before they are serialized (M15).

    Redacting the already-serialized JSON misses a secret whose characters JSON
    escapes (a quote or backslash), so the payload tree is scrubbed first.
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    return value


class LLMOrchestrator:
    """Owns call-level policy and the ``llm_calls``/budget rows for model calls.

    ``CapabilityProbeService`` writes its own probe rows, and an error row
    (``_record_error_call``) has no matching budget row because no billable
    result exists.
    """

    def __init__(
        self,
        *,
        store: StorePort,
        clock: ClockPort,
        provider: Any,
        settings: Settings | None = None,
        budget: BudgetLedger | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        breaker: CircuitBreaker | None = None,
        semaphores: dict[str, threading.BoundedSemaphore] | None = None,
        runtime: LLMRuntimeRegistry | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._provider = provider
        self._settings = settings
        self._provider_name = self._resolve_provider_name(provider)
        self._budget = budget or BudgetLedger(store=store, settings=settings)
        self._sleep = sleep or _default_sleep
        self._jitter = jitter or _default_jitter
        self._runtime = runtime
        self.breaker = (
            runtime.breaker if runtime is not None else (breaker or CircuitBreaker(clock=clock))
        )
        self._semaphores = dict(semaphores or {})
        self._semaphore_lock = threading.Lock()

    @staticmethod
    def _resolve_provider_name(provider: Any) -> str:
        name = getattr(provider, "provider", None)
        if isinstance(name, str) and name:
            return name
        capabilities = provider.capabilities()
        return str(getattr(capabilities, "provider", "unknown"))

    def _resolve_model(self, role: LLMRole) -> str | None:
        if self._settings is None:
            return None
        model = self._settings.model_for_role(role.value)
        if model:
            return model
        if self._provider_name == "stub":
            return self._settings.llm_model or None
        return None

    def _semaphore(self, provider: str) -> threading.BoundedSemaphore:
        if self._runtime is not None:
            return self._runtime.semaphore(provider)
        with self._semaphore_lock:
            existing = self._semaphores.get(provider)
            if existing is not None:
                return existing
            limit = 2
            if provider == "deepseek":
                limit = 1
            if self._settings is not None:
                limit = (
                    self._settings.llm_max_concurrency_deepseek
                    if provider == "deepseek"
                    else self._settings.llm_max_concurrency
                )
            created = threading.BoundedSemaphore(limit)
            self._semaphores[provider] = created
            return created

    def complete_with_validation(
        self,
        *,
        request: LLMRequest,
        decision_id: str,
        role: LLMRole,
    ) -> OrchestrationResult:
        now = utc_timestamp(self._clock.now())
        if not request.model.strip():
            resolved = self._resolve_model(role)
            if not resolved:
                raise LLMConfigurationError(
                    "LLM_MODEL_NOT_CONFIGURED",
                    f"no model is configured for role {role.value}; no default model is shipped",
                )
            request = replace(request, model=resolved)
        if not request.request_id:
            request = replace(request, request_id=f"req_{uuid.uuid4().hex}")
        calls: list[LLMCallRecord] = []
        budgets: list[BudgetEntry] = []

        first = self._invoke(
            request=request,
            attempt=1,
            decision_id=decision_id,
            now=now,
            calls=calls,
            budgets=budgets,
        )
        final = first
        if first.refusal:
            outcome = ValidationOutcome(
                status="REJECTED",
                output=None,
                errors=(ValidationIssue("output", "LLM_REFUSED", "the model refused the request"),),
                raw_output_hash=hash_text(first.raw_text),
            )
        elif first.truncated:
            outcome = ValidationOutcome(
                status="REJECTED",
                output=None,
                errors=(
                    ValidationIssue(
                        "output", "LLM_OUTPUT_TRUNCATED", "the model output was truncated"
                    ),
                ),
                raw_output_hash=hash_text(first.raw_text),
            )
        else:

            def repair(message: str) -> str:
                nonlocal final
                repair_request = replace(
                    request,
                    user_prompt=self._repair_prompt(request, message),
                    repair_of=request.request_id,
                )
                final = self._invoke(
                    request=repair_request,
                    attempt=2,
                    decision_id=decision_id,
                    now=now,
                    calls=calls,
                    budgets=budgets,
                )
                return final.raw_text

            outcome = validate_output(first.raw_text, role=role.value, repair=repair)

        if outcome.status == "REJECTED":
            self._audit_rejection(
                request=request,
                result=first,
                outcome=outcome,
                decision_id=decision_id,
                now=now,
            )
        return OrchestrationResult(
            outcome=outcome,
            calls=tuple(calls),
            budgets=tuple(budgets),
            first_result=first,
            final_result=final,
        )

    def _invoke(
        self,
        *,
        request: LLMRequest,
        attempt: int,
        decision_id: str,
        now: str,
        calls: list[LLMCallRecord],
        budgets: list[BudgetEntry],
    ) -> LLMResult:
        provider = self._provider_name
        timeout = timeout_for_role(request.role)
        attempt_request = replace(request, timeout_seconds=timeout)
        last_error: LLMAdapterError | None = None
        for retry_index in range(_RETRY_ATTEMPTS):
            try:
                permit = self.breaker.before_call(provider)
            except CircuitOpenError:
                if last_error is None:
                    raise
                # The breaker opened between retries. Preserve the real failure
                # that tripped it: record the attempt and surface its typed code
                # instead of a misleading 429 (S4).
                self._record_error_call(
                    request=attempt_request,
                    attempt=attempt,
                    decision_id=decision_id,
                    now=now,
                    calls=calls,
                    error_code=last_error.code,
                )
                raise last_error from None
            try:
                semaphore = self._semaphore(provider)
                if not semaphore.acquire(timeout=timeout):
                    raise LLMTimeoutError("no LLM concurrency slot became available")
                result: LLMResult | None = None
                error: LLMAdapterError | None = None
                try:
                    try:
                        result = self._provider.complete(attempt_request)
                    except LLMSchemaUnsupported as exc:
                        # A provider that rejects strict schemas is retried once
                        # in the degraded JSON mode, if the adapter offers one
                        # (M12). A clone failure is its own recorded failure (F7).
                        try:
                            result = self._degraded_retry(attempt_request, provider, now)
                        except LLMAdapterError as clone_exc:
                            # The clone attempt is billed too: record it, feed the
                            # breaker, and surface its typed code (F7).
                            self._record_error_call(
                                request=attempt_request,
                                attempt=attempt,
                                decision_id=decision_id,
                                now=now,
                                calls=calls,
                                error_code=clone_exc.code,
                            )
                            if clone_exc.retryable:
                                permit.failure()
                            raise
                        else:
                            if result is None:
                                error = exc
                    except LLMAdapterError as exc:
                        error = exc
                finally:
                    semaphore.release()
                if result is not None:
                    permit.success()
                    self._record_call(
                        request=attempt_request,
                        result=result,
                        attempt=attempt,
                        decision_id=decision_id,
                        now=now,
                        calls=calls,
                        budgets=budgets,
                    )
                    return result
                assert error is not None
                if error.retryable and retry_index + 1 < _RETRY_ATTEMPTS:
                    permit.failure()
                    last_error = error
                    # The slot is already released, so a rate-limited DeepSeek
                    # call does not block the only concurrency slot while it
                    # backs off.
                    delay = _RETRY_BASE_SECONDS * (2**retry_index) + self._jitter()
                    self._sleep(delay)
                    continue
                if error.retryable:
                    permit.failure()
                self._record_error_call(
                    request=attempt_request,
                    attempt=attempt,
                    decision_id=decision_id,
                    now=now,
                    calls=calls,
                    error_code=error.code,
                )
                raise error
            finally:
                # Resolve the half-open probe on every early exit: semaphore
                # timeout, non-recording adapter failure, or an unexpected
                # exception (F1). A recorded success/failure is a no-op here.
                permit.abandon()
        # The loop always returns or raises; kept for the type checker.
        raise LLMTransportError("the provider request failed", retryable=True)

    def _degraded_retry(self, request: LLMRequest, provider: str, now: str) -> LLMResult | None:
        factory = getattr(self._provider, "degraded_clone", None)
        if not callable(factory):
            return None
        clone = factory()
        if clone is None:
            return None
        # A clone failure propagates as its typed error so the orchestrator can
        # record it, feed the breaker, and never surface a stale 422 (F7).
        result: LLMResult = clone.complete(request)
        max_output_tokens = 4096
        capabilities = getattr(clone, "capabilities", None)
        if callable(capabilities):
            max_output_tokens = int(getattr(capabilities(), "max_output_tokens", 4096))
        self._store.save_provider_capability(
            ProviderCapabilityRecord(
                provider=provider,
                model=request.model,
                strict_schema=False,
                degraded=True,
                max_output_tokens=max_output_tokens,
                status="OK",
                error_code=None,
                notes="degraded: JSON mode fallback",
                latency_ms=result.latency_ms,
                checked_at=now,
            )
        )
        return result

    def _unique_call_id(self, decision_id: str, attempt: int) -> str:
        """Allocate a call id that does not collide with a recorded invocation.

        The success path allocates under the store's ``BEGIN IMMEDIATE``
        transaction, which is collision-free. Error rows use
        ``ON CONFLICT DO NOTHING``, so a concurrent error row for the same
        decision/attempt may silently drop a duplicate rather than be billed.
        """
        base = stable_call_id(decision_id, attempt)
        existing = {call.call_id for call in self._store.list_llm_calls(decision_id=decision_id)}
        existing.update(
            record.call_id for record in self._store.list_budget_records(decision_id=decision_id)
        )
        if base not in existing:
            return base
        candidate = f"{base}-{uuid.uuid4().hex[:8].upper()}"
        while candidate in existing:
            candidate = f"{base}-{uuid.uuid4().hex[:8].upper()}"
        return candidate

    def _record_call(
        self,
        *,
        request: LLMRequest,
        result: LLMResult,
        attempt: int,
        decision_id: str,
        now: str,
        calls: list[LLMCallRecord],
        budgets: list[BudgetEntry],
    ) -> None:
        # A provider-controlled id is redacted and bounded before persistence
        # so a hostile provider cannot echo a key into SQLite/API (S6).
        request_id = sanitize_provider_id(result.request_id or request.request_id, self._secrets())
        record = LLMCallRecord(
            call_id=stable_call_id(decision_id, attempt),
            decision_id=decision_id,
            role=request.role.value,
            provider=result.provider,
            model=result.model,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            prompt_hash=rendered_prompt_hash(
                request.prompt_id,
                request.prompt_version,
                request.system_prompt,
                request.user_prompt,
            ),
            input_hash=request.input_hash,
            attempt=attempt,
            request_id=request_id,
            status="OK",
            raw_output_hash=hash_text(result.raw_text),
            usage_json=json.dumps(result.usage.to_contract(), sort_keys=True),
            latency_ms=result.latency_ms,
            error_code=None,
            created_at=now,
            system_prompt_id=request.system_prompt_id,
            system_prompt_version=request.system_prompt_version,
            system_prompt_hash=request.system_prompt_hash,
        )
        estimate = self._estimate_override(request)
        stored_call, entry = self._budget.record_billed_call(
            call=record,
            result=result,
            occurred_at=now,
            **estimate,
        )
        calls.append(stored_call)
        budgets.append(entry)

    def _estimate_override(self, request: LLMRequest) -> dict[str, Any]:
        """Honor an explicit priced estimate from a provider adapter.

        Real adapters and the stub report ``unpriced``; those defer to the
        operator ``PriceTable``. A provider that returns a priced estimate is a
        test/replay seam and is recorded verbatim rather than invented.
        """
        estimate_cost = getattr(self._provider, "estimate_cost", None)
        if not callable(estimate_cost):
            return {}
        try:
            estimate = estimate_cost(request, request.model)
        except (AttributeError, NotImplementedError, TypeError, ValueError) as exc:
            # Fall back to the operator table, but never silently: a broken
            # estimate seam must be visible in the logs (S3).
            LOGGER.warning("llm_estimate_cost_failed", error=type(exc).__name__)
            return {}
        unpriced = getattr(estimate, "unpriced", True)
        if unpriced:
            return {}
        return {
            "unpriced": False,
            "cost_usd": getattr(estimate, "amount_usd", "0"),
            "price_version": getattr(estimate, "price_version", "none/v1"),
        }

    def _record_error_call(
        self,
        *,
        request: LLMRequest,
        attempt: int,
        decision_id: str,
        now: str,
        calls: list[LLMCallRecord],
        error_code: str,
    ) -> None:
        result = LLMResult(
            provider=self._provider_name,
            model=request.model,
            raw_text="",
            usage=LLMUsage(),
            request_id=request.request_id,
        )
        record = LLMCallRecord(
            call_id=self._unique_call_id(decision_id, attempt),
            decision_id=decision_id,
            role=request.role.value,
            provider=result.provider,
            model=result.model,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            prompt_hash=rendered_prompt_hash(
                request.prompt_id,
                request.prompt_version,
                request.system_prompt,
                request.user_prompt,
            ),
            input_hash=request.input_hash,
            attempt=attempt,
            request_id=sanitize_provider_id(request.request_id, self._secrets()),
            status="ERROR",
            raw_output_hash=hash_text(""),
            usage_json=json.dumps(result.usage.to_contract(), sort_keys=True),
            latency_ms=0,
            error_code=error_code,
            created_at=now,
            system_prompt_id=request.system_prompt_id,
            system_prompt_version=request.system_prompt_version,
            system_prompt_hash=request.system_prompt_hash,
        )
        self._store.save_llm_call(record)
        calls.append(record)

    def _repair_prompt(self, request: LLMRequest, message: str) -> str:
        schema = request.schema_json
        return (
            f"{request.user_prompt}\n\nYour previous output failed validation: {message}. "
            f"Return only a corrected JSON object matching this schema:\n{schema}"
        )

    def _audit_rejection(
        self,
        *,
        request: LLMRequest,
        result: LLMResult,
        outcome: ValidationOutcome,
        decision_id: str,
        now: str,
    ) -> None:
        error_code = outcome.errors[0].code if outcome.errors else "LLM_SCHEMA_INVALID"
        payload = {
            "provider": result.provider,
            "model": result.model,
            "role": request.role.value,
            "error_code": error_code,
            "request_id": sanitize_provider_id(request.request_id, self._secrets()),
            "provider_request_id": sanitize_provider_id(result.request_id, self._secrets()),
            "raw_output_hash": outcome.raw_output_hash,
        }
        cleaned = _redact_value(payload, self._secrets())
        self._store.append_audit_event(
            AuditEventDraft(
                event_type="LLM_REJECTED",
                actor={"kind": "SYSTEM", "id": "llm_orchestrator"},
                subject={"kind": "LLM_DECISION", "id": decision_id},
                payload=cast(dict[str, Any], cleaned),
                created_at=now,
            )
        )

    def _secrets(self) -> tuple[str, ...]:
        if self._settings is None:
            return ()
        values: list[str] = []
        for name in (
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "api_token",
        ):
            secret = getattr(self._settings, name, None)
            getter = getattr(secret, "get_secret_value", None)
            if callable(getter):
                value = getter()
                if isinstance(value, str) and value.strip():
                    values.append(value)
        return tuple(values)


__all__ = [
    "ANALYSIS_TIMEOUT_SECONDS",
    "CapabilityProbeService",
    "CircuitBreaker",
    "CircuitOpenError",
    "ENTRY_RECHECK_TIMEOUT_SECONDS",
    "LLMConfigurationError",
    "LLMOrchestrator",
    "LLMRuntimeRegistry",
    "OrchestrationResult",
    "REAL_PROVIDERS",
    "capability_status",
    "model_catalog",
    "provider_catalog",
    "provider_api_key",
    "provider_configured",
    "role_models",
    "set_default_adapter_factory",
    "timeout_for_role",
]


_KNOWN_PROVIDERS = ("openai", "anthropic", "deepseek", "stub")


def capability_status(record: ProviderCapabilityRecord | None) -> str:
    """The CLI/API provider status chip."""
    if record is None:
        return "UNCHECKED"
    if record.status == "FAILED":
        return "FAILED"
    return "STRICT" if record.strict_schema else "DEGRADED"


class CapabilityProbeService:
    """Run one minimal structured probe and persist the recorded capability."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StorePort,
        clock: ClockPort,
        adapter_factory: Callable[[Settings, str], Any] | None = None,
        runtime: LLMRuntimeRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._adapter_factory = adapter_factory
        self._runtime = runtime

    def _factory(self) -> Callable[[Settings, str], Any]:
        if self._adapter_factory is not None:
            return self._adapter_factory
        return _registered_adapter_factory()

    def run(
        self, provider: str, role: LLMRole | str = LLMRole.ANALYSIS
    ) -> ProviderCapabilityRecord:
        name = provider.strip().lower()
        resolved_role = role if isinstance(role, LLMRole) else LLMRole(str(role).strip().upper())
        if name not in _KNOWN_PROVIDERS:
            raise LLMConfigurationError(
                "LLM_PROVIDER_NOT_CONFIGURED", f"unknown LLM provider {name!r}"
            )
        if name != "stub" and not provider_configured(self._settings, name):
            raise LLMConfigurationError(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"no {name} API key is configured; the probe cannot run",
            )
        model = self._settings.model_for_role(resolved_role.value)
        if model is None and name == "stub":
            model = self._settings.llm_model or None
        if not model:
            raise LLMConfigurationError(
                "LLM_MODEL_NOT_CONFIGURED",
                f"no model is configured for role {resolved_role.value}; no default is shipped",
            )
        adapter = self._factory()(self._settings, name)
        probe = getattr(adapter, "probe", None) if adapter is not None else None
        if adapter is None or not callable(probe):
            raise LLMConfigurationError(
                "LLM_PROVIDER_NOT_CONFIGURED",
                f"provider {name!r} cannot be constructed",
            )
        outcome = self._probe(adapter, probe, name, model, resolved_role)
        checked_at = utc_timestamp(self._clock.now())
        # Provider-controlled error text must never reach SQLite, the API, or a
        # terminal unfiltered: apply the same printable/redacted/200-cap rule
        # as provider ids (D1).
        notes = sanitize_provider_id(outcome.notes, self._secrets())
        record = ProviderCapabilityRecord(
            provider=name,
            model=model,
            strict_schema=bool(outcome.strict_schema),
            degraded=bool(outcome.degraded),
            max_output_tokens=int(outcome.max_output_tokens),
            status="FAILED" if outcome.error_code else "OK",
            error_code=outcome.error_code,
            notes=notes,
            latency_ms=int(outcome.latency_ms),
            checked_at=checked_at,
        )
        self._store.save_provider_capability(record)
        # A probe that never reached the provider is not a billable call: skip
        # the call/budget/audit writes so no phantom row or usage audit appears.
        if getattr(outcome, "request_made", True):
            self._record_probe_call(
                provider=name, model=model, outcome=outcome, checked_at=checked_at
            )
        return record

    def _probe(
        self,
        adapter: Any,
        probe: Callable[..., Any],
        provider: str,
        model: str,
        role: LLMRole,
    ) -> Any:
        """Run the probe under the shared breaker/semaphore when one is injected."""
        if self._runtime is None:
            return probe(model=model, role=role)
        try:
            permit = self._runtime.breaker.before_call(provider)
        except CircuitOpenError as exc:
            return ProbeOutcome(
                strict_schema=False,
                notes="circuit open",
                error_code=exc.code,
                max_output_tokens=_adapter_max_tokens(adapter),
                request_made=False,
            )
        semaphore = self._runtime.semaphore(provider)
        if not semaphore.acquire(timeout=45.0):
            permit.abandon()
            return ProbeOutcome(
                strict_schema=False,
                notes="no probe concurrency slot became available",
                error_code="LLM_TIMEOUT",
                max_output_tokens=_adapter_max_tokens(adapter),
                request_made=False,
            )
        try:
            try:
                outcome = probe(model=model, role=role)
            except BaseException:
                permit.abandon()
                raise
        finally:
            semaphore.release()
        if outcome.error_code is None:
            permit.success()
        elif outcome.error_code in {"LLM_RATE_LIMITED", "UPSTREAM_UNAVAILABLE", "LLM_TIMEOUT"}:
            permit.failure()
        else:
            # Auth/schema/output failures do not count toward the transport
            # threshold, but the half-open probe must still be released (N5/F1).
            permit.abandon()
        return outcome

    def _secrets(self) -> tuple[str, ...]:
        if self._settings is None:
            return ()
        values: list[str] = []
        for name in (
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "api_token",
        ):
            secret = getattr(self._settings, name, None)
            getter = getattr(secret, "get_secret_value", None)
            if callable(getter):
                value = getter()
                if isinstance(value, str) and value.strip():
                    values.append(value)
        return tuple(values)

    def _record_probe_call(
        self,
        *,
        provider: str,
        model: str,
        outcome: Any,
        checked_at: str,
    ) -> None:
        """Persist the billable probe atomically as a call row + budget row (M8/F4)."""
        usage = outcome.usage or LLMUsage()
        call_id = f"CALL-PROBE-{uuid.uuid4().hex[:16].upper()}"
        request_id = sanitize_provider_id(getattr(outcome, "request_id", ""), self._secrets())
        record = LLMCallRecord(
            call_id=call_id,
            decision_id=None,
            role="CAPABILITY_PROBE",
            provider=provider,
            model=model,
            prompt_id="capability_probe",
            prompt_version=1,
            prompt_hash="",
            input_hash="capability-probe",
            attempt=1,
            request_id=request_id,
            status="ERROR" if outcome.error_code else "OK",
            raw_output_hash=sanitize_provider_id(
                getattr(outcome, "raw_output_hash", ""), self._secrets()
            ),
            usage_json=json.dumps(usage.to_contract(), sort_keys=True),
            latency_ms=int(outcome.latency_ms),
            error_code=outcome.error_code,
            created_at=checked_at,
        )
        result = LLMResult(
            provider=provider,
            model=model,
            raw_text="",
            usage=usage,
            request_id=request_id,
            usage_reported=bool(getattr(outcome, "usage_reported", False)),
        )
        # A corrupt price table is handled inside the budget service as an
        # unpriced row plus a local audit, never as a probe 500 (F5).
        BudgetLedger(store=self._store, settings=self._settings).record_billed_call(
            call=record,
            result=result,
            occurred_at=checked_at,
            usage=usage,
        )


REAL_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "deepseek")


def capability_payload(record: ProviderCapabilityRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "status": capability_status(record),
        "strict_schema": record.strict_schema,
        "degraded": record.degraded,
        "checked_at": record.checked_at,
        "notes": record.notes,
    }


def provider_catalog(settings: Settings, store: StorePort) -> list[dict[str, Any]]:
    """Provider presence, role models, and the recorded capability chip."""
    capabilities = {record.provider: record for record in store.list_provider_capabilities()}
    roles = role_models(settings)
    rows: list[dict[str, Any]] = []
    for name in REAL_PROVIDERS:
        configured = provider_configured(settings, name)
        rows.append(
            {
                "provider": name,
                "configured": configured,
                "key_source": "env" if configured else "unset",
                "roles": dict(roles),
                "capability": capability_payload(capabilities.get(name)),
            }
        )
    return rows


def model_catalog(settings: Settings, provider: str) -> list[dict[str, Any]]:
    """The price catalog plus role-configured models the table does not price."""
    table = settings.price_table()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for price in table.models_for(provider):
        seen.add(price.model)
        rows.append(
            {
                "model": price.model,
                "input_per_mtok": format(price.input_per_mtok, "f"),
                "output_per_mtok": format(price.output_per_mtok, "f"),
                "cache_read_per_mtok": format(price.cache_read_per_mtok, "f"),
                "cache_write_per_mtok": format(price.cache_write_per_mtok, "f"),
                "price_version": table.version,
                "unpriced": False,
                "source_url": price.source_url,
                "as_of": price.as_of,
            }
        )
    for model in settings.model_for_role_map().values():
        if not model or model in seen:
            continue
        seen.add(model)
        rows.append(
            {
                "model": model,
                "input_per_mtok": "0",
                "output_per_mtok": "0",
                "cache_read_per_mtok": "0",
                "cache_write_per_mtok": "0",
                "price_version": table.version,
                "unpriced": True,
                "source_url": "",
                "as_of": "",
            }
        )
    return rows
