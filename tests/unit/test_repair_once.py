"""Orchestrator: one repair, retry policy, mode flags, and rejection audit (D5)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_common import LLMAuthError, LLMTimeoutError, LLMTransportError
from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_ledger import BudgetLedger
from oft.application.llm_orchestrator import LLMOrchestrator
from oft.application.llm_validation import schema_text
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult, LLMRole, LLMUsage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _SequenceProvider:
    def __init__(self, items: Sequence[Any], *, provider: str = "stub") -> None:
        self._items = list(items)
        self.requests: list[LLMRequest] = []
        self._provider = provider

    def capabilities(self) -> Any:
        from oft.ports.llm import ProviderCapabilities

        return ProviderCapabilities(provider=self._provider, strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        item = self._items[min(len(self.requests), len(self._items)) - 1]
        if isinstance(item, Exception):
            raise item
        return LLMResult(
            provider=self._provider,
            model=request.model,
            raw_text=item,
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            request_id=f"provider-{len(self.requests)}",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        raise NotImplementedError


def _request(*, role: LLMRole = LLMRole.ANALYSIS, request_id: str = "REQ-1") -> LLMRequest:
    return LLMRequest(
        role=role,
        model="fixture",
        system_prompt="system",
        user_prompt="user <market_data>{}</market_data>",
        schema_json=schema_text(),
        input_hash="input-hash",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="prompt-hash",
        request_id=request_id,
        system_prompt_id="analysis:system",
        system_prompt_hash="system-hash",
    )


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _orchestrator(
    store: SqliteStore,
    provider: _SequenceProvider,
    *,
    sleep: Any = None,
    clock: Any = None,
) -> LLMOrchestrator:
    return LLMOrchestrator(
        store=store,
        clock=clock or FixedClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=sleep or (lambda _seconds: None),
        jitter=lambda: 0.0,
    )


def test_schema_failure_is_repaired_once_with_attempt_two(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("invalid_schema.json"), _text("opportunity_valid.json")])
    orchestrator = _orchestrator(store, provider)

    result = orchestrator.complete_with_validation(
        request=_request(), decision_id="DEC-1", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "REPAIRED"
    assert len(provider.requests) == 2
    assert [call.attempt for call in result.calls] == [1, 2]
    assert len(store.list_budget_records(decision_id="DEC-1")) == 2
    # The repair prompt carries the validator error and the schema, never a key.
    assert "failed validation" in provider.requests[1].user_prompt
    assert "pattern" in provider.requests[1].user_prompt
    # The FIRST validator error from invalid_schema.json is carried verbatim, not
    # just a generic message.
    assert "'subject' is a required property" in provider.requests[1].user_prompt


def test_a_second_schema_failure_is_rejected_and_audited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("invalid_schema.json"), _text("invalid_schema.json")])

    result = _orchestrator(store, provider).complete_with_validation(
        request=_request(), decision_id="DEC-2", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "REJECTED"
    assert len(provider.requests) == 2
    events = [e for e in store.list_audit_events() if e.event_type == "LLM_REJECTED"]
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)["payload"]
    assert payload["error_code"] == "SCHEMA_INVALID"
    assert payload["raw_output_hash"]
    assert "user <market_data>" not in events[0].payload_json


def test_refusal_is_not_repaired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])
    provider.complete = lambda request: (  # type: ignore[method-assign]
        provider.requests.append(request)
        or LLMResult(provider="stub", model="fixture", raw_text="", refusal=True)
    )

    result = _orchestrator(store, provider).complete_with_validation(
        request=_request(), decision_id="DEC-3", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "REJECTED"
    assert result.outcome.errors[0].code == "LLM_REFUSED"
    assert len(provider.requests) == 1
    assert len([c for c in store.list_llm_calls(decision_id="DEC-3")]) == 1


def test_truncation_is_not_repaired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])
    provider.complete = lambda request: (  # type: ignore[method-assign]
        provider.requests.append(request)
        or LLMResult(provider="stub", model="fixture", raw_text="", truncated=True)
    )

    result = _orchestrator(store, provider).complete_with_validation(
        request=_request(), decision_id="DEC-4", role=LLMRole.ANALYSIS
    )

    assert result.outcome.errors[0].code == "LLM_OUTPUT_TRUNCATED"
    assert len(provider.requests) == 1


def test_transport_failure_retries_once_with_injected_backoff(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider(
        [LLMTransportError("boom", retryable=True), _text("opportunity_valid.json")]
    )
    sleeps: list[float] = []

    result = _orchestrator(store, provider, sleep=sleeps.append).complete_with_validation(
        request=_request(), decision_id="DEC-5", role=LLMRole.ANALYSIS
    )

    assert result.outcome.status == "VALID"
    assert len(provider.requests) == 2
    assert sleeps == [0.5]


def test_transport_failure_twice_raises_after_exactly_two_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider(
        [LLMTransportError("boom", retryable=True), LLMTransportError("boom", retryable=True)]
    )
    sleeps: list[float] = []

    with pytest.raises(LLMTransportError):
        _orchestrator(store, provider, sleep=sleeps.append).complete_with_validation(
            request=_request(), decision_id="DEC-6", role=LLMRole.ANALYSIS
        )

    assert len(provider.requests) == 2
    assert sleeps == [0.5]
    calls = store.list_llm_calls(decision_id="DEC-6")
    assert len(calls) == 1
    assert calls[0].status == "ERROR"
    assert calls[0].error_code == "UPSTREAM_UNAVAILABLE"


def test_auth_failure_is_never_retried(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([LLMAuthError("bad key")])
    sleeps: list[float] = []

    with pytest.raises(LLMAuthError):
        _orchestrator(store, provider, sleep=sleeps.append).complete_with_validation(
            request=_request(), decision_id="DEC-7", role=LLMRole.ANALYSIS
        )

    assert len(provider.requests) == 1
    assert sleeps == []


def test_empty_request_id_is_generated_and_sent_to_the_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])

    _orchestrator(store, provider).complete_with_validation(
        request=_request(request_id=""),
        decision_id="DEC-8",
        role=LLMRole.ANALYSIS,
    )

    assert provider.requests[0].request_id.startswith("req_")
    calls = store.list_llm_calls(decision_id="DEC-8")
    assert calls[0].request_id == "provider-1"


def test_entry_recheck_uses_the_45_second_timeout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("entry_recheck_enter_long.json")])

    result = _orchestrator(store, provider).complete_with_validation(
        request=_request(role=LLMRole.ENTRY_RECHECK),
        decision_id="DEC-9",
        role=LLMRole.ENTRY_RECHECK,
    )

    assert result.outcome.status == "VALID"
    assert provider.requests[0].timeout_seconds == 45.0


def test_analysis_uses_the_120_second_timeout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])

    _orchestrator(store, provider).complete_with_validation(
        request=_request(), decision_id="DEC-10", role=LLMRole.ANALYSIS
    )

    assert provider.requests[0].timeout_seconds == 120.0


def test_a_call_record_carries_prompt_and_system_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])

    _orchestrator(store, provider).complete_with_validation(
        request=_request(), decision_id="DEC-11", role=LLMRole.ANALYSIS
    )

    call = store.list_llm_calls(decision_id="DEC-11")[0]
    assert call.system_prompt_id == "analysis:system"
    assert call.system_prompt_hash == "system-hash"
    assert len(call.prompt_hash) == 64
    assert call.usage_json


class _ExhaustedSemaphore:
    def acquire(self, timeout: float | None = None) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("an unacquired semaphore must not be released")


def test_semaphore_exhaustion_is_llm_timeout_without_a_provider_call(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider([_text("opportunity_valid.json")])
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
        semaphores={"stub": _ExhaustedSemaphore()},  # type: ignore[dict-item]
    )

    with pytest.raises(LLMTimeoutError) as caught:
        orchestrator.complete_with_validation(
            request=_request(), decision_id="DEC-SEM", role=LLMRole.ANALYSIS
        )

    assert caught.value.code == "LLM_TIMEOUT"
    assert provider.requests == []


class _TrackingSemaphore:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout: float | None = None) -> bool:
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


def test_the_backoff_sleeps_after_releasing_the_semaphore(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _SequenceProvider(
        [LLMTransportError("boom", retryable=True), _text("opportunity_valid.json")]
    )
    semaphore = _TrackingSemaphore()
    observed: dict[str, int] = {}

    def sleep(_seconds: float) -> None:
        observed["acquired"] = semaphore.acquired
        observed["released"] = semaphore.released

    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(NOW),
        provider=provider,
        budget=BudgetLedger(store=store),
        sleep=sleep,
        jitter=lambda: 0.0,
        semaphores={"stub": semaphore},  # type: ignore[dict-item]
    )

    orchestrator.complete_with_validation(
        request=_request(), decision_id="DEC-BACKOFF", role=LLMRole.ANALYSIS
    )

    # The slot is released before the backoff sleep runs (M13).
    assert observed == {"acquired": 1, "released": 1}


SECRET_WITH_JSON_ESCAPES = 'opaque"secret\\value'


class _SecretRequestIdProvider:
    provider = "openai"

    def capabilities(self) -> Any:
        from oft.ports.llm import ProviderCapabilities

        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(
            provider="openai",
            model="fixture",
            raw_text="",
            refusal=True,
            request_id=SECRET_WITH_JSON_ESCAPES,
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        raise NotImplementedError


def test_audit_redaction_scrubs_values_before_serialization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        openai_api_key=SECRET_WITH_JSON_ESCAPES,
    )
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(NOW),
        provider=_SecretRequestIdProvider(),
        settings=settings,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    orchestrator.complete_with_validation(
        request=_request(), decision_id="DEC-SECRET", role=LLMRole.ANALYSIS
    )

    events = [event for event in store.list_audit_events() if event.event_type == "LLM_REJECTED"]
    assert len(events) == 1
    assert SECRET_WITH_JSON_ESCAPES not in events[0].payload_json
    assert "[REDACTED]" in events[0].payload_json


SENTINEL = "sk-SENTINEL-DO-NOT-LEAK"


class _HostileRequestIdProvider:
    provider = "openai"

    def capabilities(self) -> Any:
        from oft.ports.llm import ProviderCapabilities

        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(
            provider="openai",
            model="fixture",
            raw_text=_text("opportunity_valid.json"),
            usage=LLMUsage(input_tokens=1, output_tokens=1),
            request_id=SENTINEL + "A" * 500 + "\x00\x1b",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        raise NotImplementedError


def test_a_provider_echoed_key_is_redacted_and_capped_before_persistence(
    tmp_path: Path,
) -> None:
    """Untrusted provider ids never reach SQLite verbatim or unbounded (S6)."""
    store = _store(tmp_path)
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        openai_api_key=SENTINEL,
    )
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(NOW),
        provider=_HostileRequestIdProvider(),
        settings=settings,
        budget=BudgetLedger(store=store),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    orchestrator.complete_with_validation(
        request=_request(), decision_id="DEC-HOSTILE-ID", role=LLMRole.ANALYSIS
    )

    call = store.list_llm_calls(decision_id="DEC-HOSTILE-ID")[0]
    assert SENTINEL not in call.request_id
    assert "\x00" not in call.request_id and "\x1b" not in call.request_id
    assert len(call.request_id) <= 200
    for decision in store.list_decisions():
        assert SENTINEL not in decision.payload_json
