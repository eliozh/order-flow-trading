"""Entry re-check runs through the orchestrator: role model and priced budget (F4)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import CostEstimate, LLMRequest, LLMResult, LLMUsage

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
ENTRY_TEXT = (FIXTURES / "entry_recheck_enter_long.json").read_text(encoding="utf-8")
INVALID_TEXT = (FIXTURES / "invalid_schema.json").read_text(encoding="utf-8")
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 9, tzinfo=UTC)


class _EchoProvider:
    """A real-provider stand-in that echoes the resolved role model."""

    provider = "openai"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text=ENTRY_TEXT,
            usage=LLMUsage(input_tokens=1000, output_tokens=200),
            request_id="openai-entry-1",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        return CostEstimate(amount_usd="0", price_version="none/v1", unpriced=True)


def _settings(tmp_path: Path) -> Settings:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "version": "2026-11-09",
                "models": [
                    {
                        "provider": "openai",
                        "model": "gpt-recheck",
                        "input_per_mtok": "2.50",
                        "output_per_mtok": "10.00",
                        "cache_read_per_mtok": "1.25",
                        "cache_write_per_mtok": "3.00",
                        "source_url": "https://example.invalid",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
        llm_model_entry_recheck="gpt-recheck",
        llm_price_table=prices,
        openai_api_key="sk-test-entry",
    )


def test_entry_recheck_uses_the_role_model_and_records_a_priced_budget(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    provider = _EchoProvider()
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )

    updated = service.recheck(run.trigger.trigger_id)

    assert provider.requests
    assert provider.requests[0].model == "gpt-recheck"
    decision_id = str(updated["entry_decision_id"])
    decision = container.store.get_decision(decision_id)
    assert decision is not None
    assert decision.validation_status == "VALID"
    calls = container.store.list_llm_calls(role="ENTRY_RECHECK")
    assert calls
    assert calls[-1].model == "gpt-recheck"
    assert calls[-1].attempt == 1
    budgets = container.store.list_budget_records(decision_id=decision_id)
    assert len(budgets) == 1
    assert budgets[0].unpriced is False
    assert budgets[0].model == "gpt-recheck"
    assert budgets[0].cost_usd == "0.00450000"
    assert budgets[0].price_version == "2026-11-09"
    provenance = json.loads(decision.payload_json)["provenance"]
    assert provenance["cost"]["unpriced"] is False
    assert provenance["cost"]["amount_usd"] == "0.00450000"


class _RepairingProvider:
    """First output fails schema, the repair succeeds; both calls are billed."""

    provider = "openai"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            provider=self.provider,
            model=request.model,
            raw_text=INVALID_TEXT if self.calls == 1 else ENTRY_TEXT,
            usage=LLMUsage(input_tokens=1000, output_tokens=200),
            request_id=f"openai-entry-{self.calls}",
            latency_ms=4 if self.calls == 1 else 9,
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        return CostEstimate(amount_usd="0", price_version="none/v1", unpriced=True)


def test_a_repaired_entry_decision_sums_both_attempts_in_its_provenance(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=_RepairingProvider(),
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )

    updated = service.recheck(run.trigger.trigger_id)

    decision_id = str(updated["entry_decision_id"])
    decision = container.store.get_decision(decision_id)
    assert decision is not None
    assert decision.validation_status == "REPAIRED"
    provenance = json.loads(decision.payload_json)["provenance"]
    assert provenance["request_id"] == "openai-entry-2"
    assert provenance["latency_ms"] == 9
    assert provenance["usage"]["input_tokens"] == 2000
    assert provenance["usage"]["output_tokens"] == 400
    assert provenance["cost"]["amount_usd"] == "0.00900000"
    budgets = container.store.list_budget_records(decision_id=decision_id)
    assert len(budgets) == 2
    assert sum((Decimal(record.cost_usd) for record in budgets), Decimal("0")) == Decimal(
        "0.00900000"
    )
