"""Kill switch restart persistence: the sentinel and DB state both survive (D6)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from oft.application.entry_service import EntryService
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


def test_restart_preserves_the_kill_switch_and_open_positions(tmp_path: Path) -> None:
    container, service, kill, trigger_id = _setup(tmp_path)
    service.accept(trigger_id, idempotency_key="accept-before-restart")
    kill.activate(actor_id="test")

    restarted = KillSwitch(
        settings=container.settings, store=container.store, clock=FixedClock(NOW)
    )

    assert restarted.is_active() is True
    assert restarted.sentinel_path.exists()
    assert len(container.store.list_positions(state="OPEN")) == 1
    try:
        restarted.require_clear()
    except KillSwitchActive:
        pass
    else:  # pragma: no cover - the switch must stay closed
        raise AssertionError("restarted kill switch must block new orders")

    status = restarted.deactivate(token="RESUME", actor_id="test")

    assert status["active"] is False
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_restart_with_the_sentinel_removed_still_uses_the_db_state(tmp_path: Path) -> None:
    container, _service, kill, _trigger_id = _setup(tmp_path)
    kill.activate(actor_id="test")
    kill.sentinel_path.unlink()

    restarted = KillSwitch(
        settings=container.settings, store=container.store, clock=FixedClock(NOW)
    )

    assert restarted.is_active() is True
