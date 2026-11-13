"""Replay: the same fixed clock/inputs produce the same close ids and values."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.position_service import PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


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
    position = accepted.position
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        utc_timestamp(NOW),
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    result = service.monitor(position["position_id"])
    replay = service.monitor(position["position_id"])
    return {
        "container": container,
        "result": result,
        "replay": replay,
        "position": position,
        "settings": settings,
    }


def test_the_same_fixed_clock_and_inputs_replay_every_close_value(tmp_path: Path) -> None:
    left = _run(tmp_path / "first")
    right = _run(tmp_path / "second")

    assert left["result"].order["order_id"] == right["result"].order["order_id"]
    assert left["result"].order["client_order_id"] == right["result"].order["client_order_id"]
    assert left["result"].order["client_order_id"].startswith("oft-paper-BTCUSDT-exit-")
    assert left["result"].fill["fill_id"] == right["result"].fill["fill_id"]
    assert left["result"].fill["price"] == right["result"].fill["price"]
    assert left["result"].trade_log["trade_id"] == right["result"].trade_log["trade_id"]
    assert left["result"].trade_log["net_pnl_usd"] == right["result"].trade_log["net_pnl_usd"]
    assert left["result"].trade_log["fees_usd"] == right["result"].trade_log["fees_usd"]
    assert (
        left["result"].position["realized_pnl_usd"] == right["result"].position["realized_pnl_usd"]
    )


def test_the_replayed_close_creates_nothing_new_and_invents_no_fill(tmp_path: Path) -> None:
    run = _run(tmp_path)
    container = run["container"]
    result = run["result"]
    replay = run["replay"]

    assert result.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.trade_log is not None and result.trade_log is not None
    assert replay.trade_log["trade_id"] == result.trade_log["trade_id"]
    assert len(container.store.list_orders()) == 2
    assert len(container.store.list_fills()) == 2
    assert len(container.store.list_trade_logs(limit=None)) == 1

    # The exit fill is the deterministic mark minus the configured slippage,
    # never a fabricated price.
    slippage = Decimal(run["settings"].paper_slippage_bps) / Decimal("10000")
    expected = (Decimal(run["position"]["stop_price"]) * (Decimal("1") - slippage)).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    assert Decimal(result.fill["price"]) == expected
    assert result.fill["source"] == "PAPER"
