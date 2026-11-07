"""Shared integration helpers for contract and replay tests (not a test module)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
LLM_FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=LLM_FIXTURES,
    )


def build_loop(
    tmp_path: Path,
    *,
    entry_fixture: str | None = None,
) -> dict[str, Any]:
    resolved = settings(tmp_path)
    container = build_container(resolved, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    scan = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert trigger_run.trigger is not None
    provider = container.llm_provider
    if entry_fixture is not None:
        provider = LLMStub(
            fixtures_dir=LLM_FIXTURES,
            overrides={LLMRole.ENTRY_RECHECK: entry_fixture},
        )
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry_service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
    )
    result = entry_service.accept(trigger_run.trigger.trigger_id, idempotency_key="contract-accept")
    trigger = container.store.get_trigger_event(trigger_run.trigger.trigger_id)
    assert trigger is not None
    fresh_opportunity = container.store.get_opportunity(opportunity.opportunity_id)
    assert fresh_opportunity is not None
    decision = container.store.get_decision(json.loads(trigger.payload_json)["entry_decision_id"])
    return {
        "container": container,
        "scan": scan,
        "opportunity": json.loads(fresh_opportunity.payload_json),
        "trigger": json.loads(trigger.payload_json),
        "result": result,
        "decision": None if decision is None else json.loads(decision.payload_json),
        "budget_records": [
            json.loads(record.payload_json) for record in container.store.list_budget_records()
        ],
    }
