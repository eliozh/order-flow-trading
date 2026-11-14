"""Canonical LLM validation: parse -> schema -> Pydantic -> semantic -> one repair."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.analysis_service import AnalysisService
from oft.application.budget_ledger import BudgetLedger
from oft.application.llm_validation import MAX_RAW_OUTPUT_CHARS, validate_output
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.pricing import ModelPrice, PriceTable
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


def test_oversized_body_is_rejected_before_parsing() -> None:
    outcome = validate_output("x" * (MAX_RAW_OUTPUT_CHARS + 1), role="ANALYSIS")

    assert outcome.status == "REJECTED"
    assert outcome.output is None
    assert outcome.errors[0].code == "LLM_SCHEMA_INVALID"
    assert "exceeds" in outcome.errors[0].message


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


def test_an_accepted_opportunity_draft_is_persisted_open(tmp_path: Path) -> None:
    outcome, store = _analyze(tmp_path, _SequencedProvider(["opportunity_valid.json"]))

    assert outcome.validation_status == "VALID"
    assert outcome.opportunity is not None
    record = store.get_opportunity(outcome.opportunity.opportunity_id)
    assert record is not None
    assert record.state == "OPEN"
    payload = json.loads(record.payload_json)
    assert payload["state"] == "OPEN"
    assert [entry["to_state"] for entry in payload["state_history"]] == ["OPEN"]
    # No LLM-facing path transitioned it: only code may leave OPEN.
    assert store.list_opportunities(state="TRIGGERED") == ()
    assert store.list_opportunities(state="INVALIDATED") == ()
    assert store.list_opportunities(state="EXPIRED") == ()


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


class _UsagePerAttemptProvider:
    """Attempt 1 fails schema, attempt 2 repairs; each bills distinct usage."""

    provider = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                provider="stub",
                model="fixture",
                raw_text=_text("invalid_schema.json"),
                usage=LLMUsage(input_tokens=1_000_000, output_tokens=0),
                latency_ms=5,
                request_id="provider-attempt-1",
            )
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text=_text("opportunity_valid.json"),
            usage=LLMUsage(input_tokens=0, output_tokens=1_000_000),
            latency_ms=9,
            request_id="provider-attempt-2",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        raise NotImplementedError


def _usage_price_table() -> PriceTable:
    return PriceTable(
        version="2026-11-09",
        models={
            ("stub", "fixture"): ModelPrice(
                provider="stub",
                model="fixture",
                input_per_mtok=Decimal("2.50"),
                output_per_mtok=Decimal("10.00"),
                cache_read_per_mtok=Decimal("1.00"),
                cache_write_per_mtok=Decimal("1.00"),
                source_url="",
                as_of="2026-11-09",
            )
        },
    )


def test_repaired_decision_provenance_uses_the_final_call_and_sums_attempts(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    service = AnalysisService(
        store=store,
        clock=FixedClock(NOW),
        provider=_UsagePerAttemptProvider(),
        budget=BudgetLedger(store=store, price_table=_usage_price_table()),
    )

    outcome = service.analyze(
        symbol="BTCUSDT",
        timeframe="4h",
        metrics={
            "metric_version": "orderflow_metrics.v1",
            "data_quality": "COMPLETE",
            "quality_reasons": [],
            "gaps": [],
        },
        snapshot={"snapshot_id": "SNAP-1", "as_of": "2026-11-06T12:00:00.000Z"},
        scan_id="SCAN-1",
        report_id="RPT-1",
    )

    assert outcome.validation_status == "REPAIRED"
    provenance = outcome.decision["provenance"]
    # The final accepted call owns the request id and latency, not the rejection.
    assert provenance["request_id"] == "provider-attempt-2"
    assert provenance["latency_ms"] == 9
    assert provenance["usage"]["input_tokens"] == 1_000_000
    assert provenance["usage"]["output_tokens"] == 1_000_000
    assert provenance["usage"]["total_tokens"] == 2_000_000
    assert provenance["cost"]["amount_usd"] == "12.50000000"
    assert provenance["cost"]["unpriced"] is False

    budgets = store.list_budget_records(decision_id=outcome.decision_id)
    assert len(budgets) == 2
    assert sum((Decimal(record.cost_usd) for record in budgets), Decimal("0")) == Decimal(
        "12.50000000"
    )


def test_management_action_field_mismatch_is_a_typed_pydantic_issue() -> None:
    """M9: an action/field mismatch fails as PYDANTIC_INVALID, not silently."""
    document = json.loads(_text("management_hold.json"))
    document["position_management"]["close_fraction"] = 0.5

    outcome = validate_output(json.dumps(document), role="MANAGEMENT")

    assert outcome.status == "REJECTED"
    assert outcome.output is None
    assert outcome.errors[0].code == "PYDANTIC_INVALID"


_MANAGEMENT_RANGE_CASES = (
    pytest.param("management_partial_close.json", "close_fraction", 1.5, id="fraction-above-one"),
    pytest.param("management_partial_close.json", "close_fraction", -0.1, id="fraction-negative"),
    pytest.param("management_partial_close.json", "close_fraction", 0.0, id="fraction-zero"),
    pytest.param("management_partial_close.json", "close_fraction", 1.0, id="fraction-one"),
    pytest.param(
        "management_partial_close.json", "close_fraction", float("nan"), id="fraction-nan"
    ),
    pytest.param(
        "management_partial_close.json", "close_fraction", float("inf"), id="fraction-infinity"
    ),
    pytest.param("management_add.json", "add_risk_pct", 0.0, id="add-risk-zero"),
    pytest.param("management_add.json", "add_risk_pct", -1.0, id="add-risk-negative"),
    pytest.param("management_add.json", "add_risk_pct", 100.1, id="add-risk-above-100"),
    pytest.param("management_add.json", "add_risk_pct", float("nan"), id="add-risk-nan"),
    pytest.param("management_add.json", "add_risk_pct", float("inf"), id="add-risk-infinity"),
)


@pytest.mark.parametrize(("fixture", "field", "value"), _MANAGEMENT_RANGE_CASES)
def test_out_of_range_management_numbers_are_repairable_pydantic_issues(
    fixture: str, field: str, value: float
) -> None:
    """M9: action/field ranges are typed PYDANTIC_INVALID and the repair pass fixes them."""
    document = json.loads(_text(fixture))
    document["position_management"][field] = value

    rejected = validate_output(json.dumps(document), role="MANAGEMENT")

    assert rejected.status == "REJECTED"
    assert rejected.output is None
    assert rejected.errors[0].code == "PYDANTIC_INVALID"

    repaired = validate_output(
        json.dumps(document),
        role="MANAGEMENT",
        repair=lambda _message: _text(fixture),
    )

    assert repaired.status == "REPAIRED"
    assert repaired.output is not None
    assert repaired.repaired is True


class _RawTextProvider:
    """Returns one fixed raw document on every attempt (including the repair)."""

    provider = "stub"

    def __init__(self, raw_text: str) -> None:
        self._raw_text = raw_text
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="stub", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            provider="stub",
            model="fixture",
            raw_text=self._raw_text,
            usage=LLMUsage(),
            request_id=f"raw-{self.calls}",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> CostEstimate:
        return CostEstimate(amount_usd="0", price_version="stub/v1", unpriced=True)


def test_a_non_finite_risk_number_is_rejected_without_a_serialization_error(
    tmp_path: Path,
) -> None:
    """N1: the old path accepted NaN then crashed in ``json.dumps(allow_nan=False)``."""
    document = json.loads(_text("opportunity_valid.json"))
    document["opportunity"]["risk_request"]["risk_pct"] = float("nan")
    raw = json.dumps(document)
    assert "NaN" in raw  # the provider emitted the bare non-JSON token

    outcome, store = _analyze(tmp_path, _RawTextProvider(raw))

    assert outcome.validation_status == "REJECTED"
    assert outcome.opportunity is None
    assert any(error["code"] == "NUMBER_INVALID" for error in outcome.decision["validation_errors"])
    decision = store.get_decision(outcome.decision_id)
    assert decision is not None
    assert "NaN" not in decision.payload_json
