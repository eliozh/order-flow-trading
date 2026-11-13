"""Accept idempotency and gateway guards (no order without verdict/approval)."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryRejected, EntryResult, EntryService
from oft.application.execution_gateway import AuthContext, ExecutionGateway, GatewayError
from oft.application.kill_switch import KillSwitch, KillSwitchActive
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.risk import RiskRequest, verdict_inputs_hash
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole
from oft.ports.store import (
    DecisionRecord,
    OpportunityRecord,
    OrderRecord,
    RiskVerdictRecord,
    TriggerEventRecord,
)

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


def _container_and_trigger(tmp_path: Path, *, period: str = "4h"):
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period=period, metrics_only=False, provider="stub")
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


class _CapturingProvider:
    """Wrap the container provider and record every request it receives."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.requests: list[object] = []

    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._inner.capabilities()  # type: ignore[attr-defined]

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self._inner.complete(request)  # type: ignore[attr-defined]

    def estimate_cost(self, request, model):  # type: ignore[no-untyped-def]
        return self._inner.estimate_cost(request, model)  # type: ignore[attr-defined]


def test_entry_recheck_uses_the_scan_period_instead_of_a_hardcoded_4h(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path, period="8h")

    provider = _CapturingProvider(container.llm_provider)
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,  # type: ignore[arg-type]
        settings=container.settings,
        kill_switch=kill,
    )

    updated = service.recheck(trigger_id)

    prompts = [str(request.user_prompt) for request in provider.requests]  # type: ignore[attr-defined]
    assert prompts
    assert "Timeframe: 8h" in prompts[0]
    assert "Timeframe: 4h" not in prompts[0]
    decision = container.store.get_decision(str(updated["entry_decision_id"]))
    assert decision is not None
    assert json.loads(decision.payload_json)["subject"]["timeframe"] == "8h"


def test_entry_recheck_falls_back_to_the_source_report_timeframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path, period="8h")
    # The scan row is unavailable, but the source report still records 8h.
    monkeypatch.setattr(container.store, "get_scan", lambda scan_id: None)
    provider = _CapturingProvider(container.llm_provider)
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,  # type: ignore[arg-type]
        settings=container.settings,
        kill_switch=kill,
    )

    service.recheck(trigger_id)

    prompts = [str(request.user_prompt) for request in provider.requests]  # type: ignore[attr-defined]
    assert prompts
    assert "Timeframe: 8h" in prompts[0]


def test_entry_recheck_fails_closed_without_a_scan_period_or_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    monkeypatch.setattr(container.store, "get_scan", lambda scan_id: None)
    monkeypatch.setattr(container.store, "get_report", lambda report_id: None)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.recheck(trigger_id)

    assert excinfo.value.code == "DATA_UNAVAILABLE"


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


def test_a_second_distinct_intent_creates_a_new_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    for period in ("4h", "8h"):
        ScanService(container=container, lock=container.scan_lock).run(
            ScanRequest(symbol="BTCUSDT", period=period, metrics_only=False, provider="stub")
        )
    opportunities = container.store.list_opportunities(state="OPEN")
    assert len(opportunities) == 2
    service = _entry(container)
    order_ids: list[str] = []
    for opportunity in opportunities:
        run = TriggerService(store=container.store, clock=container.clock).evaluate(
            opportunity.opportunity_id
        )
        assert run.trigger is not None
        result = service.accept(
            run.trigger.trigger_id, idempotency_key=f"accept-{opportunity.opportunity_id}"
        )
        assert result.order is not None
        order_ids.append(str(result.order["order_id"]))

    assert len(set(order_ids)) == 2
    assert len(container.store.list_orders()) == 2
    assert len(container.store.list_fills()) == 2
    assert len(container.store.list_positions(state="OPEN")) == 2


def test_cross_trigger_idempotency_key_is_a_conflict(tmp_path: Path) -> None:
    container, trigger_a = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.accept(trigger_a, idempotency_key="shared-key")
    stamp = utc_timestamp(container.clock.now())
    container.store.save_opportunity(
        OpportunityRecord(
            opportunity_id="OPP-OTHER",
            symbol="BTCUSDT",
            state="OPEN",
            created_at=stamp,
            updated_at=stamp,
            payload_json=json.dumps({"opportunity_id": "OPP-OTHER"}),
        )
    )
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


def test_reject_after_trigger_invalidates_the_triggered_opportunity(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None
    assert opportunity.state == "TRIGGERED"
    service = _entry(container)

    service.reject(trigger_id, reason="no setup after review")

    updated = container.store.get_opportunity(trigger.opportunity_id)
    assert updated is not None
    assert updated.state == "INVALIDATED"
    events = container.store.list_opportunity_events(trigger.opportunity_id)
    assert events[-1].to_state == "INVALIDATED"


def test_far_llm_entry_price_is_blocked_by_the_slippage_cap(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry_with_fixture(container, "entry_recheck_far_mark.json")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-far-mark")

    # The advisory entry price (2.00) is ~99,971 bps away from the ~70k mark,
    # so the percentage-based gate blocks it instead of sizing from it.
    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    verdict = json.loads(trigger.payload_json)["risk_verdict"]
    assert verdict["binding_cap"] == "SLIPPAGE"
    assert verdict["verdict"] == "BLOCK"


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
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
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


def test_gateway_rejects_a_non_paper_intent(tmp_path: Path) -> None:
    container, _trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    intent = replace(_intent(), environment="LIVE")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "ORDER_REJECTED"
    assert "PAPER" in excinfo.value.message
    assert container.store.list_orders() == ()


# -- N1: NO_ENTRY and missing-intent accept branches -----------------------


def test_no_entry_recheck_rejects_without_orders_and_invalidates(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry_with_fixture(container, "entry_recheck_no_entry.json")

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-no-entry")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    assert json.loads(trigger.payload_json)["status"] == "REJECTED"
    opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None
    assert opportunity.state == "INVALIDATED"


def test_missing_entry_intent_rejects_without_orders(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    _persist_entry_decision(container, trigger_id, intent=None)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-missing-intent")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    payload = json.loads(trigger.payload_json)
    assert payload["status"] == "REJECTED"
    assert payload["rejection_reason"] == "entry intent is missing"


# -- N2: sole-submit gateway guards ----------------------------------------


def test_gateway_blocks_a_persisted_block_verdict(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container, trigger_id=trigger_id, verdict="BLOCK", allowed_quantity=None
    )
    intent = _intent(risk_verdict_id=verdict_id)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()


def test_gateway_blocks_an_expired_verdict(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(container, trigger_id=trigger_id, expires_in=-1)
    intent = _intent(risk_verdict_id=verdict_id)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "expired" in excinfo.value.message
    assert container.store.list_orders() == ()


def test_gateway_blocks_an_active_kill_switch(tmp_path: Path) -> None:
    container, _trigger_id = _container_and_trigger(tmp_path)
    gateway, kill, _broker = _gateway(container)
    kill.activate(actor_id="test")

    with pytest.raises(KillSwitchActive) as excinfo:
        gateway.submit(
            _intent(),
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "KILL_SWITCH_ACTIVE"
    assert container.store.list_orders() == ()


def test_gateway_returns_duplicate_for_a_used_idempotency_key(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    accepted = _entry(container).accept(trigger_id, idempotency_key="dup-gateway")
    order = container.store.list_orders()[0]
    order_payload = json.loads(order.payload_json)
    verdict = container.store.get_risk_verdict(order.risk_verdict_id)
    assert verdict is not None
    verdict_payload = json.loads(verdict.payload_json)
    gateway, _kill, _broker = _gateway(container)
    intent = _intent(
        symbol=order.symbol,
        quantity=accepted.order["quantity"],
        decision_id=verdict.decision_id,
        risk_verdict_id=order.risk_verdict_id,
        approval_id=order_payload["approval_id"],
        idempotency_key=order.idempotency_key,
        stop_price=Decimal(order_payload["stop_price"]),
        leverage=float(verdict_payload["allowed"]["leverage"]),
        risk_pct=float(verdict_payload["requested"]["risk_pct"]),
        risk_amount_usd=Decimal(verdict_payload["allowed"]["risk_amount_usd"]),
        # The stored intent hash binds the take-profits too; reproduce them so
        # this is a genuine same-intent retry rather than a conflict.
        take_profits=(Decimal("72000.00"),),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=order_payload["approval_id"]),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "ORDER_DUPLICATE"


def test_gateway_rejects_a_different_intent_under_a_used_key(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    accepted = _entry(container).accept(trigger_id, idempotency_key="conflict-key")
    order = container.store.list_orders()[0]
    order_payload = json.loads(order.payload_json)
    verdict = container.store.get_risk_verdict(order.risk_verdict_id)
    assert verdict is not None
    verdict_payload = json.loads(verdict.payload_json)
    gateway, _kill, _broker = _gateway(container)
    intent = _intent(
        symbol=order.symbol,
        quantity=accepted.order["quantity"],
        decision_id=verdict.decision_id,
        risk_verdict_id=order.risk_verdict_id,
        approval_id=order_payload["approval_id"],
        idempotency_key=order.idempotency_key,
        stop_price=Decimal(order_payload["stop_price"]),
        leverage=float(verdict_payload["allowed"]["leverage"]),
        risk_pct=float(verdict_payload["requested"]["risk_pct"]),
        risk_amount_usd=Decimal(verdict_payload["allowed"]["risk_amount_usd"]),
        # A different take-profit set is a different intent under the same key.
        take_profits=(Decimal("99999.00"),),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=order_payload["approval_id"]),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(container.store.list_orders()) == 1


# -- N3: the gateway binds the intent to the verdict -----------------------


def test_gateway_rejects_a_quantity_above_the_verdict_allowed_quantity(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container, trigger_id=trigger_id, allowed_quantity="0.005", decision_id="DEC-X"
    )
    intent = _intent(risk_verdict_id=verdict_id, quantity="0.010", decision_id="DEC-X")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.list_orders() == ()


def test_gateway_rejects_a_verdict_from_another_decision(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(container, trigger_id=trigger_id, decision_id="DEC-OTHER")
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "decision" in excinfo.value.message


def test_gateway_rejects_a_side_mismatch(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(container, trigger_id=trigger_id, decision_id="DEC-X")
    intent = _intent(
        risk_verdict_id=verdict_id,
        decision_id="DEC-X",
        position_side=PositionSide.SHORT,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "side" in excinfo.value.message


def test_gateway_rejects_a_verdict_whose_inputs_changed(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container, trigger_id=trigger_id, decision_id="DEC-X", inputs_hash="deadbeef"
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "inputs" in excinfo.value.message


def test_gateway_rejects_an_allowed_quantity_with_a_wider_stop(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(
        risk_verdict_id=verdict_id,
        quantity="0.010",
        decision_id="DEC-X",
        stop_price=Decimal("68000.00"),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "stop" in excinfo.value.message
    assert container.store.list_orders() == ()


def test_gateway_rejects_an_intent_risk_amount_above_the_verdict(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(
        risk_verdict_id=verdict_id,
        quantity="0.010",
        decision_id="DEC-X",
        risk_amount_usd=Decimal("25.00"),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "risk" in excinfo.value.message
    assert container.store.list_orders() == ()


def test_gateway_rejects_a_far_mark_above_the_verdict_allowed_risk(
    tmp_path: Path,
) -> None:
    """A direct caller cannot understate risk by declaring a low amount.

    The quantity, leverage, stop, and declared risk all fit the verdict, but the
    submitted mark is far from the approved entry, so the real stop-loss risk is
    ~11x the approved risk.
    """
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(
        risk_verdict_id=verdict_id,
        quantity="0.010",
        decision_id="DEC-X",
        risk_amount_usd=Decimal("10.00"),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("80000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "mark" in excinfo.value.message
    assert container.store.list_orders() == ()


def test_gateway_accepts_a_mark_that_matches_the_verdict_entry(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(
        risk_verdict_id=verdict_id,
        quantity="0.010",
        decision_id="DEC-X",
        risk_amount_usd=Decimal("10.00"),
    )

    result = gateway.submit(
        intent,
        AuthContext(actor_id="cli", approval_id="APR-X"),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
    )

    assert result.order.quantity == Decimal("0.010")
    assert len(container.store.list_orders()) == 1


def test_gateway_rejects_a_verdict_without_an_allowed_risk(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        allowed_risk_usd="0",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(
        risk_verdict_id=verdict_id,
        quantity="0.010",
        decision_id="DEC-X",
        risk_amount_usd=Decimal("10.00"),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "allowed risk" in excinfo.value.message
    assert container.store.list_orders() == ()


# -- N6/N8: intent bounds and fail-closed symbol data ----------------------


def test_negative_leverage_in_a_persisted_decision_is_rejected_not_raised(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    intent["leverage"] = -1.0
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-negative-leverage")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()


def test_zero_risk_pct_is_not_silently_replaced(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    intent["risk_pct"] = 0.0
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    # A 0% proposal sizes to zero; it must block at MIN_NOTIONAL rather than
    # being silently upsized to the per-trade cap.
    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-zero-risk")

    assert excinfo.value.code == "RISK_BLOCKED"
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    verdict = json.loads(trigger.payload_json)["risk_verdict"]
    assert verdict["requested"]["risk_pct"] == 0.0
    assert verdict["verdict"] == "BLOCK"
    assert verdict["binding_cap"] == "MIN_NOTIONAL"
    assert container.store.list_orders() == ()


def test_missing_symbol_contract_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry_with_fixture(container, "entry_recheck_enter_long.json")
    monkeypatch.setattr(container.store, "get_symbol", lambda symbol: None)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-no-contract")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert container.store.list_orders() == ()
    assert container.store.list_fills() == ()


# -- N6 (continued): size_mode is exclusive and governs the sizing ----------


def test_risk_pct_size_mode_ignores_a_quote_notional(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    # Both sizing fields are present, but the mode is RISK_PCT. The validator
    # forbids this document; a persisted intent must still honor the mode.
    intent["size_mode"] = "RISK_PCT"
    intent["risk_pct"] = 0.8
    intent["quote_notional_usd"] = "700.00"
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    result = service.accept(trigger_id, idempotency_key="accept-risk-mode")

    assert result.risk_verdict["requested"]["notional_usd"] is None
    # A notional-governed size would be 0.010; the risk-governed request must
    # hit the stricter 2,000 USD notional cap instead.
    assert Decimal(str(result.risk_verdict["allowed"]["quantity"])) == Decimal("0.028")


def test_risk_pct_size_mode_requires_a_risk_pct_on_a_persisted_intent(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    intent["size_mode"] = "RISK_PCT"
    intent["risk_pct"] = None
    intent["quote_notional_usd"] = "700.00"
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-missing-risk-pct")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()


def test_quote_notional_size_mode_requires_a_notional_on_a_persisted_intent(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    intent["size_mode"] = "QUOTE_NOTIONAL"
    intent["risk_pct"] = 0.8
    intent["quote_notional_usd"] = None
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-missing-notional")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stop_price", "not-a-number"),
        ("risk_pct", "not-a-number"),
        ("leverage", "not-a-number"),
    ],
)
def test_malformed_persisted_intent_numerics_are_rejected_not_raised(
    tmp_path: Path, field: str, value: str
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    intent = _entry_intent_document()
    intent[field] = value
    _persist_entry_decision(container, trigger_id, intent=intent)
    service = _entry(container)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-malformed-intent")

    assert excinfo.value.code == "ORDER_REJECTED"
    assert container.store.list_orders() == ()


# -- D6: the database enforces one paper entry per trigger -----------------


def _run_concurrently(attempts: int, action) -> list[object]:  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(attempts)

    def run(index: int) -> object:
        barrier.wait()
        try:
            return action(index)
        except EntryRejected as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        return list(pool.map(run, range(attempts)))


def test_six_concurrent_distinct_keys_create_at_most_one_entry(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)

    results = _run_concurrently(
        6,
        lambda index: service.accept(trigger_id, idempotency_key=f"accept-race-{index}"),
    )

    orders = container.store.list_orders()
    assert len(orders) == 1
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1
    replayed = set()
    for result in results:
        if isinstance(result, EntryResult):
            assert result.order is not None
            replayed.add(str(result.order["order_id"]))
    assert replayed == {orders[0].order_id}
    assert orders[0].trigger_id == trigger_id
    assert orders[0].purpose == "ENTRY"


def test_concurrent_same_key_requests_replay_the_single_order(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)

    results = _run_concurrently(
        6,
        lambda _index: service.accept(trigger_id, idempotency_key="same-key-race"),
    )

    assert all(isinstance(result, EntryResult) for result in results)
    order_ids = {
        str(result.order["order_id"])
        for result in results
        if isinstance(result, EntryResult) and result.order is not None
    }
    orders = container.store.list_orders()
    assert len(orders) == 1
    assert order_ids == {orders[0].order_id}
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_crash_after_the_order_commit_reconciles_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)
    original_update = container.store.update_trigger_event

    def _crash_after_commit(record, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after the OMS commit")

    monkeypatch.setattr(container.store, "update_trigger_event", _crash_after_commit)
    with pytest.raises(RuntimeError):
        service.accept(trigger_id, idempotency_key="crash-first")
    monkeypatch.setattr(container.store, "update_trigger_event", original_update)

    committed = container.store.list_orders()
    assert len(committed) == 1
    assert committed[0].trigger_id == trigger_id
    stale = container.store.get_trigger_event(trigger_id)
    assert stale is not None
    assert stale.status == "RECHECKED"
    assert json.loads(stale.payload_json)["created_order_id"] is None

    result = service.accept(trigger_id, idempotency_key="crash-retry")

    assert result.order is not None
    assert result.order["order_id"] == committed[0].order_id
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_fills()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1
    healed = container.store.get_trigger_event(trigger_id)
    assert healed is not None
    assert healed.status == "ACCEPTED"
    assert json.loads(healed.payload_json)["created_order_id"] == committed[0].order_id


def test_reject_surfaces_the_human_reason(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)

    payload = service.reject(trigger_id, reason="spread too wide for the stop")

    assert payload["rejection_reason"] == "spread too wide for the stop"
    assert payload["human_action"]["reason"] == "spread too wide for the stop"
    stored = container.store.get_trigger_event(trigger_id)
    assert stored is not None
    stored_payload = json.loads(stored.payload_json)
    assert stored_payload["rejection_reason"] == "spread too wide for the stop"
    audit = [
        event
        for event in container.store.list_audit_events(limit=10)
        if event.event_type == "TRIGGER_REJECTED"
    ]
    assert "spread too wide for the stop" in audit[0].payload_json


# -- S6/P1: crash-window reconciliation from recheck and reject -------------


def _crash_after_commit(container, trigger_id: str, service: EntryService) -> str:
    service.recheck(trigger_id)

    def _crash(record, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after the OMS commit")

    from unittest import mock

    with (
        mock.patch.object(container.store, "update_trigger_event", _crash),
        pytest.raises(RuntimeError),
    ):
        service.accept(trigger_id, idempotency_key="crash-first")
    orders = container.store.list_orders()
    assert len(orders) == 1
    return orders[0].order_id


def test_recheck_adopts_a_committed_order_without_an_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    order_id = _crash_after_commit(container, trigger_id, service)
    calls: list[object] = []
    monkeypatch.setattr(
        service._provider,  # type: ignore[union-attr]
        "complete",
        lambda request: calls.append(request),  # type: ignore[arg-type]
    )

    card = service.recheck(trigger_id)

    assert calls == []
    assert card["status"] == "ACCEPTED"
    assert card["order"]["order_id"] == order_id
    healed = container.store.get_trigger_event(trigger_id)
    assert healed is not None
    assert healed.status == "ACCEPTED"
    assert len(container.store.list_orders()) == 1


def test_reject_conflicts_with_a_committed_order_and_keeps_it_open(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    order_id = _crash_after_commit(container, trigger_id, service)
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None

    with pytest.raises(EntryRejected) as excinfo:
        service.reject(trigger_id, reason="human says no")

    assert excinfo.value.code == "CONFLICT"
    still = container.store.get_trigger_event(trigger_id)
    assert still is not None
    assert still.status == "RECHECKED"
    opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None
    assert opportunity.state == "TRIGGERED"
    assert [order.order_id for order in container.store.list_orders()] == [order_id]
    assert len(container.store.list_positions(state="OPEN")) == 1
    assert not any(
        event.event_type == "TRIGGER_REJECTED"
        for event in container.store.list_audit_events(limit=50)
    )


# -- M1: conditional trigger writes (compare-and-set) -----------------------


def test_an_in_flight_accept_cannot_overwrite_a_human_reject(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)
    stale = container.store.get_trigger_event(trigger_id)
    assert stale is not None
    service.reject(trigger_id, reason="human wins the race")
    audits_before = len(container.store.list_audit_events(limit=100))

    with pytest.raises(EntryRejected) as excinfo:
        service._update_trigger(
            stale,
            status="ACCEPTED",
            expected_status="RECHECKED",
            require_no_order=True,
        )

    assert excinfo.value.code == "CONFLICT"
    after = container.store.get_trigger_event(trigger_id)
    assert after is not None
    assert after.status == "REJECTED"
    payload = json.loads(after.payload_json)
    assert payload["human_action"]["action"] == "REJECT"
    # The lost compare-and-set wrote no audit event.
    assert len(container.store.list_audit_events(limit=100)) == audits_before


def test_a_reject_cannot_land_after_the_order_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)
    committed = threading.Event()
    original_submit = service._gateway.submit

    def _submit_then_signal(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original_submit(*args, **kwargs)
        committed.set()
        return result

    monkeypatch.setattr(service._gateway, "submit", _submit_then_signal)
    outcomes: list[object] = []

    def _accept() -> None:
        try:
            outcomes.append(service.accept(trigger_id, idempotency_key="race-accept"))
        except EntryRejected as exc:
            outcomes.append(exc.code)

    def _reject() -> None:
        assert committed.wait(timeout=5)
        try:
            outcomes.append(service.reject(trigger_id, reason="too late"))
        except EntryRejected as exc:
            outcomes.append(exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_accept), pool.submit(_reject)]
        for future in futures:
            future.result(timeout=10)

    assert "CONFLICT" in outcomes
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    assert trigger.status == "ACCEPTED"
    assert json.loads(trigger.payload_json)["human_action"]["action"] == "ACCEPT"
    assert len(container.store.list_orders()) == 1
    assert len(container.store.list_positions(state="OPEN")) == 1


# -- item 9: accept re-checks the status after the inline re-check ----------


def test_accept_after_an_inline_recheck_that_rejected_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    original = service._evaluate_and_persist_risk
    calls = {"count": 0}

    def _transient_failure(**kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            raise EntryRejected("DATA_UNAVAILABLE", "transient gate-input failure")
        return original(**kwargs)

    monkeypatch.setattr(service, "_evaluate_and_persist_risk", _transient_failure)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-inline-reject")

    # The inline re-check marked the trigger REJECTED; accept must stop there
    # instead of running a second evaluation and creating an order (P3).
    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert calls["count"] == 1
    assert container.store.list_orders() == ()
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    assert trigger.status == "REJECTED"


# -- item 10: crash-window adoption/replay fail closed on a missing verdict --


def test_adoption_fails_closed_when_the_order_verdict_is_missing(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    _crash_after_commit(container, trigger_id, service)
    order = container.store.list_orders()[0]
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute(
            "DELETE FROM risk_verdicts WHERE risk_verdict_id = ?",
            (order.risk_verdict_id,),
        )

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="crash-retry-missing-verdict")

    assert excinfo.value.code == "CONFLICT"
    assert "verdict" in excinfo.value.message
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    assert trigger.status == "RECHECKED"
    assert len(container.store.list_orders()) == 1


def test_replay_fails_closed_when_the_order_verdict_is_missing(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    accepted = service.accept(trigger_id, idempotency_key="replay-missing-verdict")
    order = container.store.list_orders()[0]
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute(
            "DELETE FROM risk_verdicts WHERE risk_verdict_id = ?",
            (order.risk_verdict_id,),
        )

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="replay-missing-verdict")

    assert excinfo.value.code == "CONFLICT"
    assert accepted.order is not None
    assert len(container.store.list_orders()) == 1


# -- item 7: adoption is PAPER-scoped --------------------------------------


def test_adoption_refuses_a_non_paper_entry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    service = _entry(container)
    service.recheck(trigger_id)
    live = OrderRecord(
        order_id="ORD-LIVE",
        client_order_id="client-live",
        environment="LIVE",
        symbol="BTCUSDT",
        status="FILLED",
        risk_verdict_id="RISK-X",
        idempotency_key="idem-live",
        created_at="2026-11-06T12:00:00.000Z",
        updated_at="2026-11-06T12:00:00.000Z",
        payload_json=json.dumps({"order_id": "ORD-LIVE", "position_id": None}),
        trigger_id=trigger_id,
        decision_id="DEC-X",
        purpose="ENTRY",
    )
    monkeypatch.setattr(container.store, "get_entry_order_for_trigger", lambda _tid: live)

    with pytest.raises(EntryRejected) as excinfo:
        service.accept(trigger_id, idempotency_key="accept-live-order")

    assert excinfo.value.code == "CONFLICT"
    assert "PAPER" in excinfo.value.message
    assert container.store.list_orders() == ()


def test_get_entry_order_for_trigger_ignores_non_paper_rows(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    with sqlite3.connect(container.settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
            "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
            "trigger_id, decision_id, purpose) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ORD-LIVE",
                "client-live",
                "LIVE",
                "BTCUSDT",
                "FILLED",
                "RISK-X",
                "idem-live",
                "2026-11-06T12:00:00.000Z",
                "2026-11-06T12:00:00.000Z",
                "{}",
                trigger_id,
                "DEC-X",
                "ENTRY",
            ),
        )
        connection.commit()

    assert container.store.get_entry_order_for_trigger(trigger_id) is None


# -- item 8: a measured zero daily loss is not "absent" --------------------


def test_gateway_keeps_a_stored_zero_daily_loss_in_the_inputs_digest(
    tmp_path: Path,
) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        daily_loss_usd="0",
        inputs_hash=_matching_inputs_hash(container, daily_loss_usd=Decimal("0")),
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X", quantity="0.010")

    result = gateway.submit(
        intent,
        AuthContext(actor_id="cli", approval_id="APR-X"),
        mark_price=Decimal("70000.00"),
        mark_as_of="2026-11-06T12:00:00.000Z",
    )

    assert result.order.order_id
    assert len(container.store.list_orders()) == 1


# -- P4: untested gateway last-line branches ------------------------------


def test_gateway_blocks_leverage_above_the_verdict(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container, trigger_id=trigger_id, decision_id="DEC-X", allowed_leverage=2.0
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X", leverage=3.0)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "leverage" in excinfo.value.message


def test_gateway_blocks_a_missing_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(container, trigger_id=trigger_id, decision_id="DEC-X")
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X")
    monkeypatch.setattr(container.store, "get_trigger_event", lambda tid: None)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "trigger" in excinfo.value.message


def test_gateway_blocks_a_symbol_mismatch(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(container, trigger_id=trigger_id, decision_id="DEC-X")
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X", symbol="ETHUSDT")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "symbol" in excinfo.value.message


def test_gateway_blocks_unparseable_verdict_inputs(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        requested={
            "entry_price": "not-a-price",
            "stop_price": "69000.00",
            "quantity": None,
            "risk_pct": 0.8,
            "leverage": 2.0,
            "notional_usd": None,
        },
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "inputs" in excinfo.value.message


def test_gateway_blocks_an_intent_without_a_protective_stop(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        requested={
            "entry_price": "70000.00",
            "stop_price": "0",
            "quantity": None,
            "risk_pct": 0.8,
            "leverage": 2.0,
            "notional_usd": None,
        },
        inputs_hash=_matching_inputs_hash(container, stop_price=None),
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X", stop_price=None)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "stop" in excinfo.value.message


def test_gateway_blocks_a_missing_mark_price(tmp_path: Path) -> None:
    container, trigger_id = _container_and_trigger(tmp_path)
    gateway, _kill, _broker = _gateway(container)
    verdict_id = _persist_verdict(
        container,
        trigger_id=trigger_id,
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    intent = _intent(risk_verdict_id=verdict_id, decision_id="DEC-X", quantity="0.010")

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("0"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "mark price" in excinfo.value.message


def test_gateway_maps_a_database_idempotency_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, trigger_a = _container_and_trigger(tmp_path)
    _entry(container).accept(trigger_a, idempotency_key="db-conflict-key")
    stamp = utc_timestamp(container.clock.now())
    container.store.save_opportunity(
        OpportunityRecord(
            opportunity_id="OPP-OTHER",
            symbol="BTCUSDT",
            state="OPEN",
            created_at=stamp,
            updated_at=stamp,
            payload_json=json.dumps({"opportunity_id": "OPP-OTHER"}),
        )
    )
    container.store.save_trigger_event(
        TriggerEventRecord(
            trigger_id="TRG-OTHER",
            opportunity_id="OPP-OTHER",
            symbol="BTCUSDT",
            status="RECHECKED",
            idempotency_key="TRG-OTHER-KEY",
            triggered_at="2026-11-06T12:00:00.000Z",
            payload_json=json.dumps(
                {
                    "status": "RECHECKED",
                    "opportunity_id": "OPP-OTHER",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                }
            ),
        )
    )
    verdict_id = _persist_verdict(
        container,
        trigger_id="TRG-OTHER",
        decision_id="DEC-X",
        inputs_hash=_matching_inputs_hash(container),
    )
    gateway, _kill, _broker = _gateway(container)
    intent = _intent(
        risk_verdict_id=verdict_id,
        decision_id="DEC-X",
        quantity="0.010",
        idempotency_key="db-conflict-key",
        take_profits=(Decimal("99999.00"),),
    )
    original = container.store.get_order_by_idempotency
    calls = {"count": 0}

    def _pass_the_precheck(key: str):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original(key)

    monkeypatch.setattr(container.store, "get_order_by_idempotency", _pass_the_precheck)

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id="APR-X"),
            mark_price=Decimal("70000.00"),
            mark_as_of="2026-11-06T12:00:00.000Z",
        )

    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(container.store.list_orders()) == 1


# -- helpers ---------------------------------------------------------------


def _gateway(container):
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    gateway = ExecutionGateway(
        store=container.store, broker=broker, kill_switch=kill, clock=container.clock
    )
    return gateway, kill, broker


def _intent(
    *,
    symbol: str = "BTCUSDT",
    quantity: str = "0.010",
    decision_id: str = "DEC-X",
    risk_verdict_id: str = "RISK-X",
    approval_id: str = "APR-X",
    idempotency_key: str = "idem-gateway",
    stop_price: Decimal = Decimal("69000.00"),
    leverage: float = 2.0,
    risk_pct: float = 0.8,
    risk_amount_usd: Decimal = Decimal("10.00"),
    position_side: PositionSide = PositionSide.LONG,
    take_profits: tuple[Decimal, ...] = (),
) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side=OrderSide.BUY if position_side is PositionSide.LONG else OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=None,
        stop_price=stop_price,
        take_profits=take_profits,
        leverage=leverage,
        decision_id=decision_id,
        risk_verdict_id=risk_verdict_id,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
        position_side=position_side,
        risk_amount_usd=risk_amount_usd,
        risk_pct=risk_pct,
    )


def _persist_verdict(
    container,
    *,
    trigger_id: str,
    decision_id: str = "DEC-X",
    verdict: str = "ALLOW",
    allowed_quantity: str | None = "0.020",
    allowed_leverage: float = 2.0,
    allowed_risk_usd: str = "20.00",
    expires_in: int = 30,
    inputs_hash: str = "deadbeef",
    daily_loss_usd: str | None = None,
    requested: dict | None = None,
) -> str:
    now = utc_timestamp(container.clock.now())
    expires_at = utc_timestamp(container.clock.now() + timedelta(seconds=expires_in))
    payload = {
        "verdict": verdict,
        "requested": (
            requested
            if requested is not None
            else {
                "entry_price": "70000.00",
                "stop_price": "69000.00",
                "quantity": None,
                "risk_pct": 0.8,
                "leverage": 2.0,
                "notional_usd": None,
            }
        ),
        "allowed": {
            "quantity": allowed_quantity,
            "notional_usd": None if allowed_quantity is None else "1400.00",
            "leverage": allowed_leverage,
            "risk_amount_usd": allowed_risk_usd,
        },
        "binding_cap": "NONE" if verdict != "BLOCK" else "MISSING_STOP",
        "reasons": [],
        "computed_at": now,
        "expires_at": expires_at,
        "inputs_hash": inputs_hash,
        "stale_inputs": [],
    }
    verdict_id = "RISK-TEST"
    # ``risk_verdicts.decision_id`` is a foreign key from schema v21; the
    # synthetic verdicts in these gateway tests need their parent decision.
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role=LLMRole.ENTRY_RECHECK.value,
            decision_type="ENTRY_RECHECK",
            validation_status="VALID",
            raw_output_hash="TEST-HASH",
            persisted_at=now,
            payload_json="{}",
        )
    )
    container.store.save_risk_verdict(
        RiskVerdictRecord(
            risk_verdict_id=verdict_id,
            trigger_id=trigger_id,
            decision_id=decision_id,
            verdict=verdict,
            binding_cap=payload["binding_cap"],
            computed_at=now,
            expires_at=expires_at,
            payload_json=json.dumps(payload),
            daily_loss_usd=daily_loss_usd,
        )
    )
    return verdict_id


def _matching_inputs_hash(
    container,
    *,
    stop_price: Decimal | None = Decimal("69000.00"),
    daily_loss_usd: Decimal | None = None,
) -> str:
    """The digest the gateway recomputes for ``_persist_verdict``'s request."""
    from oft.application.paper_account import compute_equity

    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    account = broker.account()
    positions = container.store.list_positions(state="OPEN")
    equity, _view = compute_equity(
        Decimal(account.balance),
        [json.loads(record.payload_json) for record in positions],
    )
    request = RiskRequest(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=Decimal("70000.00"),
        stop_price=stop_price,
        quantity=None,
        risk_pct=0.8,
        leverage=2.0,
        notional_usd=None,
    )
    return verdict_inputs_hash(
        request,
        equity=equity,
        open_positions=len(positions),
        daily_loss_usd=daily_loss_usd,
    )


def _entry_intent_document() -> dict:
    path = FIXTURES / "entry_recheck_enter_long.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["entry_recheck"]["order_intent"]


def _persist_entry_decision(container, trigger_id: str, *, intent: dict | None) -> str:
    decision_id = "DEC-MANUAL"
    now = utc_timestamp(container.clock.now())
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="ENTRY_RECHECK",
            decision_type="ENTRY_RECHECK",
            validation_status="VALID",
            raw_output_hash="manual",
            persisted_at=now,
            payload_json=json.dumps(
                {"entry_recheck": {"decision": "ENTER_LONG", "order_intent": intent}}
            ),
        )
    )
    trigger = container.store.get_trigger_event(trigger_id)
    assert trigger is not None
    payload = json.loads(trigger.payload_json)
    payload["entry_decision_id"] = decision_id
    container.store.update_trigger_event(
        replace(trigger, payload_json=json.dumps(payload, sort_keys=True))
    )
    return decision_id
