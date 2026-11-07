"""Initial deterministic replay check for the paper loop (full harness in slice 21)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _run(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    container = replace(container, clock=FixedClock(NOW))
    scan = ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert trigger_run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    result = entry.accept(trigger_run.trigger.trigger_id, idempotency_key="replay-accept")
    stored_trigger = container.store.get_trigger_event(trigger_run.trigger.trigger_id)
    assert stored_trigger is not None
    trigger_payload = json.loads(stored_trigger.payload_json)
    decision = container.store.get_decision(str(trigger_payload["entry_decision_id"]))
    return {
        "report_id": scan.report.report_id,
        "decision": None if decision is None else json.loads(decision.payload_json),
        "opportunity": json.loads(opportunity.payload_json),
        "trigger": trigger_payload,
        "order": result.order,
        "fill": result.fill,
        "position": result.position,
        "account": result.account,
    }


def _stable(payload: dict[str, Any]) -> dict[str, Any]:
    opportunity = json.loads(json.dumps(payload["opportunity"]))
    opportunity["source"] = {
        key: value
        for key, value in opportunity["source"].items()
        if key not in {"scan_id", "report_id"}
    }
    return {
        "decision_id": payload["decision"]["decision_id"],
        "decision_validation": payload["decision"]["validation_status"],
        "opportunity": opportunity,
        "trigger": payload["trigger"],
        "order": payload["order"],
        "fill": payload["fill"],
        "position": payload["position"],
        "account": payload["account"],
    }


def test_same_fixture_and_fixed_clock_produce_identical_paper_loop_values(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    left = _stable(first)
    right = _stable(second)

    assert left == right
    assert left["order"]["client_order_id"].startswith("oft-paper-BTCUSDT-entry-")
    assert left["order"]["order_id"] == right["order"]["order_id"]
    assert left["fill"]["fill_id"] == right["fill"]["fill_id"]
    assert left["position"]["position_id"] == right["position"]["position_id"]
    assert (
        left["trigger"]["snapshot"]["snapshot_hash"]
        == right["trigger"]["snapshot"]["snapshot_hash"]
    )


def test_replay_does_not_invent_a_provider_or_credential(tmp_path: Path) -> None:
    payload = _run(tmp_path)

    assert payload["decision"]["provenance"]["provider"] == "stub"
    assert payload["decision"]["provenance"]["model"] == "fixture"
    assert payload["decision"]["provenance"]["cost"]["unpriced"] is True
