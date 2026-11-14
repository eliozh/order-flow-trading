"""Replay: the same fixed clock and inputs produce the same management result."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


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
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="replay-entry")
    assert accepted.position is not None
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=container.settings,
        kill_switch=kill,
    )
    result = service.manage(accepted.position["position_id"], idempotency_key="replay-manage")
    record = container.store.get_position(accepted.position["position_id"])
    assert record is not None
    return {
        "container": container,
        "result": result,
        "position": json.loads(record.payload_json),
    }


@pytest.mark.replay
def test_replaying_the_same_management_inputs_is_byte_identical(tmp_path: Path) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first["result"].decision_id == second["result"].decision_id
    assert first["result"].order is not None and second["result"].order is not None
    assert first["result"].order["order_id"] == second["result"].order["order_id"]
    assert first["result"].fill is not None and second["result"].fill is not None
    assert first["result"].fill["fill_id"] == second["result"].fill["fill_id"]
    assert first["position"]["size"] == second["position"]["size"]
    assert (
        first["position"]["management_history"][-1]["decision_id"]
        == second["position"]["management_history"][-1]["decision_id"]
    )
    assert first["container"].store.list_trade_logs(limit=None) == ()


@pytest.mark.replay
def test_a_replayed_management_call_creates_no_new_rows(tmp_path: Path) -> None:
    state = _run(tmp_path)
    container = state["container"]
    orders = container.store.count_orders()
    fills = container.store.count_fills()

    again = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=container.settings,
        kill_switch=KillSwitch(
            settings=container.settings, store=container.store, clock=container.clock
        ),
    ).manage(state["position"]["position_id"], idempotency_key="replay-manage")

    assert again.idempotent_replay is True
    assert container.store.count_orders() == orders
    assert container.store.count_fills() == fills
