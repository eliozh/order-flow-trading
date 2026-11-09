"""Real provider adapters run through the scan LLM stage (F2)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_common import LLMAuthError
from oft.application.scan_service import ScanRequest, ScanService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult, LLMRole, LLMUsage

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
VALID_TEXT = (ROOT / "tests" / "fixtures" / "llm" / "analysis_valid.json").read_text(
    encoding="utf-8"
)
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 9, tzinfo=UTC)


class _FakeProvider:
    """A real-provider stand-in that echoes the resolved role model."""

    provider = "openai"

    def __init__(self, *, raw_text: str = VALID_TEXT, error: Exception | None = None) -> None:
        self._raw_text = raw_text
        self._error = error
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text=self._raw_text,
            usage=LLMUsage(input_tokens=100, output_tokens=50),
            request_id="openai-request-1",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        from oft.ports.llm import CostEstimate

        return CostEstimate(amount_usd="0", price_version="none/v1", unpriced=True)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="openai",
        openai_api_key="sk-SENTINEL-DO-NOT-LEAK",
        llm_model_analysis="gpt-fixture",
    )


def _scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> Any:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "oft.bootstrap.build_llm_adapter",
        lambda _settings, name: provider if (name or "").strip().lower() == "openai" else None,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    run = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False)
    )
    return container, run


def test_a_real_provider_runs_through_the_scan_llm_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()

    container, run = _scan(tmp_path, monkeypatch, provider)

    assert provider.requests
    assert provider.requests[0].role is LLMRole.ANALYSIS
    assert provider.requests[0].model == "gpt-fixture"
    assert run.scan.status == "DONE"
    assert run.llm_state["configured"] is True
    assert run.llm_state["validation_status"] == "VALID"
    report = json.loads(run.report.llm_json or "{}")
    assert report["configured"] is True
    calls = container.store.list_llm_calls(role="ANALYSIS")
    assert len(calls) == 1
    assert calls[0].model == "gpt-fixture"
    budgets = container.store.list_budget_records()
    assert len(budgets) == 1
    assert budgets[0].model == "gpt-fixture"


def test_a_failing_provider_degrades_to_a_calc_report_with_the_mapped_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider(error=LLMAuthError("no openai API key is configured"))

    container, run = _scan(tmp_path, monkeypatch, provider)

    assert run.scan.status == "DONE"
    assert run.report.metrics_json
    assert run.llm_state["validation_status"] == "REJECTED"
    assert str(run.llm_state["reason"]).startswith("LLM_STAGE_FAILED")
    assert "no openai API key is configured" in str(run.llm_state["reason"])
    assert container.store.list_opportunities() == ()
    assert container.store.list_orders() == ()
