"""BLOCK/REDUCE semantics: a blocked entry never creates an order (D2)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
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


def _setup(tmp_path: Path, entry_fixture: str) -> tuple[Any, EntryService, str]:
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
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.ENTRY_RECHECK: entry_fixture},
    )
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
    )
    return container, service, run.trigger.trigger_id


def test_a_blocked_entry_creates_no_order_fill_or_position(tmp_path: Path) -> None:
    container, service, trigger_id = _setup(tmp_path, "entry_recheck_stopless.json")

    card = service.recheck(trigger_id)
    assert card["risk"]["verdict"] == "BLOCK"
    assert card["risk"]["binding_cap"] == "MISSING_STOP"

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-stopless-block")

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    assert container.store.list_positions() == ()
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    assert trigger.status == "BLOCKED"
    verdict = json.loads(trigger.payload_json)["risk_verdict"]
    assert verdict["verdict"] == "BLOCK"
    assert verdict["allowed"]["quantity"] is None


def test_a_blocked_trigger_cannot_be_retried_into_an_order(tmp_path: Path) -> None:
    container, service, trigger_id = _setup(tmp_path, "entry_recheck_stopless.json")
    with pytest.raises(EntryRejected):
        service.accept(trigger_id, idempotency_key="accept-block-first")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-block-second")

    assert excinfo.value.code == "CONFLICT"
    assert container.store.list_orders() == ()


def test_reduce_creates_an_order_at_the_allowed_quantity_only(tmp_path: Path) -> None:
    container, service, trigger_id = _setup(tmp_path, "entry_recheck_oversized.json")

    result = service.accept(trigger_id, idempotency_key="accept-reduce")

    assert result.risk_verdict["verdict"] == "REDUCE"
    assert result.risk_verdict["binding_cap"] == "MAX_NOTIONAL"
    allowed = Decimal(str(result.risk_verdict["allowed"]["quantity"]))
    order = container.store.list_orders()[0]
    payload = json.loads(order.payload_json)
    assert Decimal(str(payload["quantity"])) == allowed
    assert Decimal(str(payload["quantity"])) <= Decimal("0.028")
