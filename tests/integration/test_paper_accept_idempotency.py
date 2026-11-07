"""Accept idempotency and gateway guards (no order without verdict/approval)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


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


def test_repeat_accept_with_the_same_key_creates_nothing_new(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)

    first = service.accept(trigger_id, idempotency_key="accept-idem")
    second = service.accept(trigger_id, idempotency_key="accept-idem")

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.order is not None and second.order is not None
    assert first.order["order_id"] == second.order["order_id"]
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


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
