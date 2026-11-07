"""Canonical LLM validation: parse -> schema -> Pydantic -> semantic -> one repair."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.analysis_service import AnalysisService
from oft.application.llm_validation import validate_output
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import CostEstimate, LLMRequest, LLMResult, LLMUsage, ProviderCapabilities

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_valid_fixture_passes_every_pipeline_stage() -> None:
    outcome = validate_output(_text("opportunity_valid.json"), role="ANALYSIS")

    assert outcome.status == "VALID"
    assert outcome.output is not None
    assert outcome.output.decision_type == "OPPORTUNITY_PROPOSAL"
    assert outcome.errors == ()


def test_analysis_fixture_without_an_opportunity_is_valid() -> None:
    outcome = validate_output(_text("analysis_valid.json"), role="ANALYSIS")

    assert outcome.status == "VALID"
    assert outcome.output is not None
    assert outcome.output.opportunity is None


def test_invalid_schema_is_rejected() -> None:
    outcome = validate_output(_text("invalid_schema.json"), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.output is None
    assert outcome.errors[0].code in {"SCHEMA_INVALID", "PAYLOAD_MISSING"}


def test_refusal_is_rejected() -> None:
    outcome = validate_output(_text("refusal.json"), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.output is None


def test_truncated_output_is_rejected() -> None:
    outcome = validate_output(_text("truncated.json"), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.output is None


def test_markdown_fence_is_rejected_without_fuzzy_parsing() -> None:
    outcome = validate_output(f"```json\n{_text('opportunity_valid.json')}\n```", role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.errors[0].code == "LLM_SCHEMA_INVALID"


def test_empty_body_is_rejected_as_truncated() -> None:
    outcome = validate_output("   ", role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.errors[0].code == "LLM_OUTPUT_TRUNCATED"


def test_provider_supplied_provenance_is_rejected_as_an_unknown_field() -> None:
    document = json.loads(_text("opportunity_valid.json"))
    document["provenance"] = {"provider": "forged"}

    outcome = validate_output(json.dumps(document), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.output is None


def test_semantic_stop_side_failure_is_rejected() -> None:
    document = json.loads(_text("opportunity_valid.json"))
    document["opportunity"]["risk_request"]["stop_price"] = "72000.00"

    outcome = validate_output(json.dumps(document), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert any(error.code == "STOP_SIDE" for error in outcome.errors)


def test_wrong_role_decision_type_is_rejected() -> None:
    outcome = validate_output(_text("opportunity_valid.json"), role="ENTRY_RECHECK")

    assert outcome.status == "REJECTED"
    assert any(error.code == "DECISION_TYPE_ROLE_MISMATCH" for error in outcome.errors)


class _SequencedProvider:
    """Returns fixtures in order; records every call for attempt assertions."""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        index = min(self.calls, len(self._names) - 1)
        self.calls += 1
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text=_text(self._names[index]),
            usage=LLMUsage(),
            request_id=f"stub-{index}",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _analyze(tmp_path: Path, provider: _SequencedProvider):
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    service = AnalysisService(
        store=store,
        clock=FixedClock(NOW),
        provider=provider,
    )
    return service.analyze(
        symbol="BTCUSDT",
        timeframe="4h",
        metrics={
            "metric_version": "orderflow_metrics.v1",
            "data_quality": "COMPLETE",
            "quality_reasons": [],
            "gaps": [],
        },
        snapshot={
            "snapshot_id": "SNAP-1",
            "snapshot_hash": "a" * 64,
            "as_of": "2026-11-06T12:00:00.000Z",
            "last_price": "70002.00",
        },
        scan_id="SCAN-1",
        report_id="RPT-1",
    ), store


def test_one_repair_creates_a_second_call_and_persists_a_repaired_decision(
    tmp_path: Path,
) -> None:
    outcome, store = _analyze(
        tmp_path, _SequencedProvider(["invalid_schema.json", "opportunity_valid.json"])
    )

    assert outcome.validation_status == "REPAIRED"
    assert outcome.opportunity is not None
    calls = store.list_llm_calls(decision_id=outcome.decision_id)
    assert [call.attempt for call in calls] == [1, 2]
    assert len(store.list_budget_records(decision_id=outcome.decision_id)) == 2
    assert all(
        record.unpriced for record in store.list_budget_records(decision_id=outcome.decision_id)
    )
    decision = store.get_decision(outcome.decision_id)
    assert decision is not None
    assert decision.validation_status == "REPAIRED"


def test_a_second_failure_is_rejected_and_creates_no_opportunity(tmp_path: Path) -> None:
    outcome, store = _analyze(
        tmp_path, _SequencedProvider(["invalid_schema.json", "invalid_schema.json"])
    )

    assert outcome.validation_status == "REJECTED"
    assert outcome.opportunity is None
    assert outcome.opportunity_ids == ()
    assert store.list_opportunities() == ()
    calls = store.list_llm_calls(decision_id=outcome.decision_id)
    assert [call.attempt for call in calls] == [1, 2]


def test_refusal_flag_is_rejected_without_a_repair_call(tmp_path: Path) -> None:
    class _RefusingProvider(_SequencedProvider):
        def complete(self, request: LLMRequest) -> LLMResult:
            self.calls += 1
            return LLMResult(provider="stub", model="fixture", raw_text="", refusal=True)

    outcome, store = _analyze(tmp_path, _RefusingProvider(["opportunity_valid.json"]))

    assert outcome.validation_status == "REJECTED"
    assert outcome.opportunity is None
    assert len(store.list_llm_calls(decision_id=outcome.decision_id)) == 1


def test_code_assigns_provenance_and_subject_even_when_the_model_omits_them(
    tmp_path: Path,
) -> None:
    outcome, _store = _analyze(tmp_path, _SequencedProvider(["opportunity_valid.json"]))

    decision = outcome.decision
    assert decision["provenance"]["provider"] == "stub"
    assert decision["provenance"]["model"] == "fixture"
    assert decision["subject"]["scan_id"] == "SCAN-1"
    assert decision["subject"]["report_id"] == "RPT-1"
    assert decision["subject"]["environment"] == "PAPER"


def test_opportunity_conversion_failure_degrades_to_a_rejected_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import InvalidOperation

    from oft.application import analysis_service

    def _boom(self, **kwargs):  # type: ignore[no-untyped-def]
        raise InvalidOperation("not a decimal")

    monkeypatch.setattr(analysis_service.OpportunityService, "create_from_decision", _boom)

    outcome, store = _analyze(tmp_path, _SequencedProvider(["opportunity_valid.json"]))

    assert outcome.validation_status == "REJECTED"
    assert outcome.opportunity is None
    assert outcome.opportunity_ids == ()
    decision = store.get_decision(outcome.decision_id)
    assert decision is not None
    assert decision.validation_status == "REJECTED"
    assert store.list_opportunities() == ()
