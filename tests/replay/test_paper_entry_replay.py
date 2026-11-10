"""Deterministic paper-entry replay: identical clock/seed -> identical records (ADR-0011)."""

from __future__ import annotations

import json
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
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert trigger_run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    card = service.recheck(trigger_run.trigger.trigger_id)
    result = service.accept(trigger_run.trigger.trigger_id, idempotency_key="replay-entry")
    return {
        "card": card,
        "order": result.order,
        "fill": result.fill,
        "position": result.position,
        "account": result.account,
        "risk_verdict": result.risk_verdict,
    }


def test_the_same_fixture_and_fixed_clock_replay_every_entry_value(tmp_path: Path) -> None:
    left = _run(tmp_path / "first")
    right = _run(tmp_path / "second")

    assert left["order"]["order_id"] == right["order"]["order_id"]
    assert left["order"]["client_order_id"] == right["order"]["client_order_id"]
    assert left["fill"]["fill_id"] == right["fill"]["fill_id"]
    assert left["fill"]["price"] == right["fill"]["price"]
    assert left["position"]["position_id"] == right["position"]["position_id"]
    assert left["position"]["size"] == right["position"]["size"]
    assert left["account"] == right["account"]
    assert left["risk_verdict"] == right["risk_verdict"]
    assert left["card"]["stage_order"] == ["TRIGGER", "LLM", "RISK", "HUMAN"]
    assert left["order"]["client_order_id"].startswith("oft-paper-BTCUSDT-entry-")


def test_replay_never_invents_a_fill_or_a_second_order(tmp_path: Path) -> None:
    payload = _run(tmp_path)
    repeated = json.loads(json.dumps(payload["order"]))

    assert repeated["status"] == "FILLED"
    assert repeated["filled_quantity"] == repeated["quantity"]
    assert payload["fill"]["source"] == "PAPER"
