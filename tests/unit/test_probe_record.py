"""Capability probe validation and call/budget recording (M7, M8, M9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from oft.adapters.llm_anthropic import AnthropicAdapter
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.sqlite_store import SqliteStore
from oft.application.llm_orchestrator import CapabilityProbeService
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMUsage, ProbeOutcome

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "providers" / "openai"


def _adapter(body: dict[str, Any], status: int = 200) -> OpenAIAdapter:
    return OpenAIAdapter(
        api_key="sk-SENTINEL-DO-NOT-LEAK",
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body)),
    )


def test_anthropic_successful_probe_is_strict() -> None:
    body = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": '{"ok": "ok"}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    adapter = AnthropicAdapter(
        api_key="sk-SENTINEL-DO-NOT-LEAK",
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )

    outcome = adapter.probe(model="claude-fixture")

    assert outcome.error_code is None
    assert outcome.strict_schema is True
    assert outcome.degraded is False
    assert outcome.notes == "strict schema accepted"
    assert outcome.max_output_tokens == adapter.capabilities().max_output_tokens
    assert outcome.request_id == "msg_probe"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("refusal", "LLM_REFUSED"),
        ("truncated", "LLM_OUTPUT_TRUNCATED"),
        ("empty_content", "LLM_OUTPUT_TRUNCATED"),
        ("malformed", "LLM_SCHEMA_INVALID"),
    ],
)
def test_a_200_probe_body_is_validated_not_assumed_strict(case: str, code: str) -> None:
    body = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))

    outcome = _adapter(body).probe(model="gpt-fixture")

    assert outcome.strict_schema is False
    assert outcome.error_code == code


def test_a_successful_probe_reports_the_advertised_output_capability() -> None:
    body = {
        "id": "resp_probe",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"ok": "ok"}'}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    adapter = _adapter(body)

    outcome = adapter.probe(model="gpt-fixture")

    assert outcome.error_code is None
    assert outcome.max_output_tokens == adapter.capabilities().max_output_tokens
    assert outcome.max_output_tokens != 64
    assert outcome.usage is not None
    assert outcome.raw_output_hash
    assert outcome.request_id


class _ProbeAdapter:
    def __init__(self, outcome: ProbeOutcome) -> None:
        self._outcome = outcome

    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        return self._outcome


def test_the_probe_call_and_budget_row_are_persisted(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        openai_api_key="sk-SENTINEL-DO-NOT-LEAK",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
    )
    store = SqliteStore(settings)
    store.initialize()
    from oft.ports.llm import LLMUsage

    outcome = ProbeOutcome(
        strict_schema=True,
        notes="strict schema accepted",
        max_output_tokens=4096,
        latency_ms=12,
        usage=LLMUsage(input_tokens=10, output_tokens=5),
        raw_output_hash="hash",
        request_id="req-probe",
    )
    service = CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: _ProbeAdapter(outcome),
    )

    service.run("openai")

    calls = store.list_llm_calls(role="CAPABILITY_PROBE")
    budgets = store.list_budget_records()
    assert len(calls) == 1
    assert calls[0].attempt == 1
    assert calls[0].status == "OK"
    assert calls[0].request_id == "req-probe"
    assert len(budgets) == 1
    assert budgets[0].role == "CAPABILITY_PROBE"
    # A probe is not a decision.
    assert store.list_decisions() == ()


def _price_table(tmp_path: Path) -> Path:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "version": "v",
                "models": [
                    {
                        "provider": "openai",
                        "model": "gpt-fixture",
                        "input_per_mtok": "2.50",
                        "output_per_mtok": "10.00",
                        "cache_read_per_mtok": "1.00",
                        "cache_write_per_mtok": "1.00",
                        "source_url": "",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return prices


def _probe_with(tmp_path: Path, outcome: ProbeOutcome) -> SqliteStore:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
        openai_api_key="sk-test",
        llm_price_table=_price_table(tmp_path),
    )
    store = SqliteStore(settings)
    store.initialize()
    CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: _ProbeAdapter(outcome),
    ).run("openai")
    return store


def test_a_probe_without_reported_usage_is_unpriced_and_audited(tmp_path: Path) -> None:
    """A probe that carried no usage object is never priced at $0 (F6)."""
    from oft.ports.llm import LLMUsage

    store = _probe_with(
        tmp_path,
        ProbeOutcome(
            strict_schema=True,
            notes="strict schema accepted",
            max_output_tokens=4096,
            latency_ms=5,
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            usage_reported=False,
        ),
    )

    budget = store.list_budget_records()[0]
    assert budget.unpriced is True
    assert budget.cost_usd == "0"
    assert budget.price_version == "none/v1"
    events = [e for e in store.list_audit_events() if e.event_type == "LLM_USAGE_MISSING"]
    assert len(events) == 1


def test_a_probe_with_reported_usage_is_priced(tmp_path: Path) -> None:
    from oft.ports.llm import LLMUsage

    store = _probe_with(
        tmp_path,
        ProbeOutcome(
            strict_schema=True,
            notes="strict schema accepted",
            max_output_tokens=4096,
            latency_ms=5,
            usage=LLMUsage(input_tokens=1_000_000, output_tokens=0),
            usage_reported=True,
        ),
    )

    budget = store.list_budget_records()[0]
    assert budget.unpriced is False
    assert budget.cost_usd == "2.50000000"


class _PricedProbeAdapter:
    """A probe seam whose successive outcomes carry priced usage."""

    def __init__(self, usages: list[LLMUsage]) -> None:
        self._usages = iter(usages)

    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        return ProbeOutcome(
            strict_schema=True,
            notes="strict schema accepted",
            max_output_tokens=4096,
            latency_ms=5,
            usage=next(self._usages),
            usage_reported=True,
        )


class _RecordingBudgetSink:
    """An in-memory BudgetAlertSink; no Telegram, keyring, or network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def enqueue_budget_threshold(
        self,
        *,
        period: str,
        period_key: str,
        spend_usd: str,
        threshold_usd: str,
        state: str,
        occurred_at: str,
    ) -> None:
        self.calls.append(
            {
                "period": period,
                "period_key": period_key,
                "spend_usd": spend_usd,
                "threshold_usd": threshold_usd,
                "state": state,
                "occurred_at": occurred_at,
            }
        )


def test_a_probe_that_crosses_the_threshold_alerts_exactly_once(tmp_path: Path) -> None:
    """A billable probe crossing 80% must reach the sink exactly once (ME-1)."""
    adapter = _PricedProbeAdapter(
        [
            LLMUsage(input_tokens=3_000_000, output_tokens=0),  # $7.50 -> OK
            LLMUsage(input_tokens=400_000, output_tokens=0),  # $8.50 total -> WARN
            LLMUsage(input_tokens=40_000, output_tokens=0),  # $8.60 total -> still WARN
        ]
    )
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
        openai_api_key="sk-test",
        llm_price_table=_price_table(tmp_path),
    )
    store = SqliteStore(settings)
    store.initialize()
    sink = _RecordingBudgetSink()
    service = CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: adapter,
        notifications=sink,
    )

    service.run("openai")  # 75% of the $10.00 daily threshold: no crossing
    service.run("openai")  # 85%: one crossing
    service.run("openai")  # 86%, still above: no second alert

    assert [(call["period"], call["state"]) for call in sink.calls] == [("day", "WARN")]
    assert sink.calls[0]["period_key"] == "2026-11-09"
    assert Decimal(sink.calls[0]["spend_usd"]) == Decimal("8.50")
    assert Decimal(sink.calls[0]["threshold_usd"]) == Decimal("10.00")
    assert len(store.list_budget_records()) == 3


def _runtime_with_open_breaker() -> Any:
    from oft.application.llm_orchestrator import LLMRuntimeRegistry

    runtime = LLMRuntimeRegistry(clock=FixedClock(NOW))
    for _ in range(3):
        runtime.breaker.record_failure("openai")
    return runtime


def test_a_probe_blocked_by_an_open_circuit_records_no_request(tmp_path: Path) -> None:
    """No provider request means no call row, budget row, or usage audit (silent-3 N1)."""
    from oft.application.budget_service import BudgetService

    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
        openai_api_key="sk-test",
    )
    store = SqliteStore(settings)
    store.initialize()
    outcome = ProbeOutcome(strict_schema=True, usage_reported=True)
    service = CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: _ProbeAdapter(outcome),
        runtime=_runtime_with_open_breaker(),
    )
    before = BudgetService(store=store).usage("day", at="2026-11-09T12:00:00.000Z")

    record = service.run("openai")

    assert record.status == "FAILED"
    assert record.error_code == "LLM_RATE_LIMITED"
    assert store.list_llm_calls() == ()
    assert store.list_budget_records() == ()
    audits = [
        event for event in store.list_audit_events() if event.event_type == "LLM_USAGE_MISSING"
    ]
    assert audits == []
    after = BudgetService(store=store).usage("day", at="2026-11-09T12:00:00.000Z")
    assert after["call_count"] == before["call_count"] == 0


def test_a_provider_echoed_key_is_redacted_and_capped_in_the_probe_row(
    tmp_path: Path,
) -> None:
    sentinel = "sk-SENTINEL-DO-NOT-LEAK"
    outcome = ProbeOutcome(
        strict_schema=True,
        notes="strict schema accepted",
        request_id=sentinel + "A" * 500,
        usage_reported=False,
    )
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="openai",
        llm_model_analysis="gpt-fixture",
        openai_api_key=sentinel,
    )
    store = SqliteStore(settings)
    store.initialize()

    CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: _ProbeAdapter(outcome),
    ).run("openai")

    call = store.list_llm_calls(role="CAPABILITY_PROBE")[0]
    assert sentinel not in call.request_id
    assert len(call.request_id) <= 200
