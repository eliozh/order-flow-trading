"""Accept idempotency and gateway guards (no order without verdict/approval)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.risk import DEFAULT_CAPS
from oft.ports.llm import LLMRole
from oft.ports.store import TriggerEventRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _container_and_trigger(tmp_path: Path):
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True)
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
    return container, run.trigger.trigger_id


def _entry(container):
    from oft.application.entry_service import EntryService

    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )


def _entry_with_fixture(container, fixture: str):
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.ENTRY_RECHECK: fixture},
    )
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
    )


def test_repeat_accept_with_the_same_key_creates_nothing_new(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)

    first = service.accept(trigger_id, idempotency_key="accept-idem")
    second = service.accept(trigger_id, idempotency_key="accept-idem")

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.order is not None and second.order is not None
    assert first.order["order_id"] == second.order["order_id"]
    # The replay must reproduce the original response in full, not just the id.
    assert replace(second, idempotent_replay=False) == first
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_replay_finds_the_fill_by_order_id_not_a_recent_page(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    first = service.accept(trigger_id, idempotency_key="accept-fill-lookup")

    order_id = str(first.order["order_id"])
    assert container.store.get_fill_by_order_id(order_id) is not None
    second = service.accept(trigger_id, idempotency_key="accept-fill-lookup")

    assert second.fill == first.fill


def test_cross_trigger_idempotency_key_is_a_conflict(tmp_path: Path) -> None:
    container, trigger_a = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.accept(trigger_a, idempotency_key="shared-key")
    container.store.save_trigger_event(
        TriggerEventRecord(
            trigger_id="TRG-OTHER",
            opportunity_id="OPP-OTHER",
            symbol="BTCUSDT",
            status="PENDING_RECHECK",
            idempotency_key="TRG-OTHER-KEY",
            triggered_at="2026-11-06T12:00:00.000Z",
            payload_json=json.dumps(
                {
                    "status": "PENDING_RECHECK",
                    "entry_decision_id": "DEC-OTHER",
                    "opportunity_id": "OPP-OTHER",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                }
            ),
        )
    )

    with pytest.raises(EntryRejected) as excinfo:
        service.accept("TRG-OTHER", idempotency_key="shared-key")

    assert excinfo.value.code == "CONFLICT"
    assert len(container.store.list_orders()) == 1


def test_reject_does_not_require_an_llm_provider(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=None,
        settings=container.settings,
        kill_switch=kill,
    )

    result = service.reject(trigger_id, reason="no setup after review")

    assert result["status"] == "REJECTED"
    assert container.store.list_orders() == ()


def test_far_llm_entry_price_cannot_enlarge_the_risk_caps(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry_with_fixture(container, "entry_recheck_far_mark.json")

    result = service.accept(trigger_id, idempotency_key="accept-far-mark")

    allowed = result.risk_verdict["allowed"]
    allowed_quantity = Decimal(str(allowed["quantity"]))
    allowed_notional = Decimal(str(allowed["notional_usd"]))
    # Sized from the fill reference (~70k), not the advisory entry_price of 2.
    assert allowed_quantity <= Decimal("0.01")
    assert allowed_notional <= DEFAULT_CAPS.max_notional_usd


def test_limit_entry_type_is_rejected_and_creates_no_order(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    opportunities_before = container.store.count_opportunities()
    service = _entry_with_fixture(container, "entry_recheck_far_mark_limit.json")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-far-mark-limit")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    assert container.store.count_opportunities() == opportunities_before


def test_stale_report_mark_blocks_the_entry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True)
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    container = replace(container, clock=FixedClock(NOW))
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
        clock=FixedClock(NOW + timedelta(seconds=120)),
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(run.trigger.trigger_id, idempotency_key="accept-stale-mark")

    assert excinfo.value.code == "RISK_BLOCKED"
    triggers = container.store.list_trigger_events(opportunity_id=opportunity.opportunity_id)
    risk_verdict = json.loads(triggers[0].payload_json)["risk_verdict"]
    assert risk_verdict["binding_cap"] == "STALE_MARK"


def test_gateway_requires_a_persisted_risk_verdict(tmp_path: Path) -> None:
    container, _trigger_id = _container_and_trigger(tmp_path)
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    gateway = ExecutionGateway(
        store=container.store, broker=broker, kill_switch=kill, clock=container.clock
    )
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("69000.00"),
        take_profits=(),
        leverage=2.0,
        decision_id="DEC-X",
        risk_verdict_id="RISK-MISSING",
        approval_id="APR-X",
        idempotency_key="idem-missing-verdict",
        position_side=PositionSide.LONG,
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()


def test_gateway_requires_human_approval(tmp_path: Path) -> None:
    container, _trigger_id = _container_and_trigger(tmp_path)
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    gateway = ExecutionGateway(
        store=container.store, broker=broker, kill_switch=kill, clock=container.clock
    )
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("69000.00"),
        take_profits=(),
        leverage=2.0,
        decision_id="DEC-X",
        risk_verdict_id="RISK-X",
        approval_id="APR-X",
        idempotency_key="idem-no-approval",
        position_side=PositionSide.LONG,
        risk_amount_usd=Decimal("80.00"),
        risk_pct=0.8,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=None),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "ORDER_REJECTED"
