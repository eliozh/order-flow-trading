"""Kill switch blocks new paper orders and never closes open positions (D6)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch, KillSwitchActive
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
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _setup(tmp_path: Path) -> tuple[Any, EntryService, KillSwitch, str]:
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
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    return container, service, kill, run.trigger.trigger_id


def test_kill_switch_blocks_a_new_paper_order(tmp_path: Path) -> None:
    container, service, kill, trigger_id = _setup(tmp_path)
    service.recheck(trigger_id)
    kill.activate(actor_id="test")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-killed-new")

    assert excinfo.value.code == "KILL_SWITCH_ACTIVE"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    assert container.store.list_positions() == ()


def test_open_positions_survive_kill_switch_activation(tmp_path: Path) -> None:
    container, service, kill, trigger_id = _setup(tmp_path)
    service.accept(trigger_id, idempotency_key="accept-before-kill")
    assert len(container.store.list_positions(state="OPEN")) == 1

    kill.activate(actor_id="test")

    assert len(container.store.list_positions(state="OPEN")) == 1
    with pytest.raises(KillSwitchActive):
        kill.require_clear()
