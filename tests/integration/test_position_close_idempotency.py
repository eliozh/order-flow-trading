"""Idempotency, kill-switch classification, and gateway guards for a close."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.application.entry_service import EntryService
from oft.application.execution_gateway import (
    AuthContext,
    ExecutionGateway,
    GatewayError,
)
from oft.application.kill_switch import KillSwitch, KillSwitchActive
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.position_service import PositionCloseError, PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    ClosedPositionRecord,
    DecisionRecord,
    FillRecord,
    OmsConflictError,
    OrderRecord,
    PaperAccountRecord,
    PositionEventRecord,
    RiskVerdictRecord,
    TradeLogRecord,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
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
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="idem-entry")
    assert accepted.position is not None
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    return {
        "container": container,
        "service": service,
        "kill": kill,
        "position": accepted.position,
        "trigger_id": run.trigger.trigger_id,
    }


def _set_mark(container: Any, position_id: str, mark: str, *, as_of: str) -> None:
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["mark_price"] = mark
        payload["updated_at"] = as_of
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def _touch_stop(setup: dict[str, Any]) -> None:
    position = setup["position"]
    _set_mark(
        setup["container"],
        position["position_id"],
        position["stop_price"],
        as_of=utc_timestamp(NOW),
    )


def _close(setup: dict[str, Any]) -> Any:
    _touch_stop(setup)
    return setup["service"].monitor(setup["position"]["position_id"])


def _gateway(container: Any) -> tuple[ExecutionGateway, KillSwitch]:
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    gateway = ExecutionGateway(
        store=container.store, broker=broker, kill_switch=kill, clock=container.clock
    )
    return gateway, kill


def _exit_intent(
    position: dict[str, Any],
    *,
    idempotency_key: str,
    risk_verdict_id: str,
    decision_id: str,
    quantity: str | None = None,
) -> OrderIntent:
    side = PositionSide(str(position["side"]))
    return OrderIntent(
        symbol=position["symbol"],
        side=OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity or position["size"]),
        price=None,
        stop_price=Decimal(str(position["stop_price"])),
        take_profits=(),
        leverage=float(position["leverage"]),
        decision_id=decision_id,
        risk_verdict_id=risk_verdict_id,
        approval_id="APR-TEST",
        idempotency_key=idempotency_key,
        position_side=side,
        risk_amount_usd=Decimal("0"),
        risk_pct=0.0,
        purpose="exit",
        position_id=position["position_id"],
        reduce_only=True,
        trigger_id=position.get("source_trigger_id"),
    )


def test_replaying_the_same_inputs_returns_the_original_and_creates_nothing(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position_id = setup["position"]["position_id"]

    first = _close(setup)
    second = setup["service"].monitor(position_id)

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.trade_log is not None and second.trade_log is not None
    assert first.trade_log["trade_id"] == second.trade_log["trade_id"]
    assert first.order is not None and second.order is not None
    assert first.order["order_id"] == second.order["order_id"]
    assert len(container.store.list_orders()) == 2  # entry + exactly one exit
    assert len(container.store.list_fills()) == 2
    assert len(container.store.list_trade_logs(limit=None)) == 1
    assert container.store.count_positions() == 1


def test_a_different_payload_under_the_same_key_is_an_idempotency_conflict(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    _close(setup)
    trade = container.store.get_trade_log_for_position(position["position_id"])
    assert trade is not None
    gateway, _kill = _gateway(container)
    intent = _exit_intent(
        position,
        idempotency_key=trade.idempotency_key,
        risk_verdict_id=trade.risk_verdict_id,
        decision_id=trade.decision_id,
        quantity=str(Decimal(position["size"]) / 2),
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=intent.approval_id),
            mark_price=Decimal(str(position["stop_price"])),
            mark_as_of=utc_timestamp(NOW),
        )

    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(container.store.list_orders()) == 2


def test_sqlite_enforces_a_unique_client_order_id(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    _close(setup)
    exit_order = next(order for order in container.store.list_orders() if order.purpose == "EXIT")
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO orders (order_id, client_order_id, environment, symbol, status, "
            "risk_verdict_id, idempotency_key, created_at, updated_at, payload_json, "
            "trigger_id, decision_id, purpose) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'EXIT')",
            (
                "ORD-DUPLICATE",
                exit_order.client_order_id,
                exit_order.environment,
                exit_order.symbol,
                "FILLED",
                exit_order.risk_verdict_id,
                "idem-duplicate-client-id",
                exit_order.created_at,
                exit_order.updated_at,
                "{}",
            ),
        )


def test_kill_switch_permits_the_reduce_only_close_and_audits_the_classification(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    _touch_stop(setup)
    setup["kill"].activate(actor_id="test")

    result = setup["service"].monitor(position["position_id"])

    assert result.status == "CLOSED"
    assert container.store.get_trade_log_for_position(position["position_id"]) is not None
    classifications = [
        json.loads(event.payload_json).get("payload", {})
        for event in container.store.list_audit_events(limit=100)
        if event.event_type == "INTENT_CLASSIFIED"
    ]
    assert any(entry["risk_reducing"] is True for entry in classifications)
    assert any(entry["risk_reducing"] is False for entry in classifications)


def test_kill_switch_still_blocks_a_risk_increasing_intent(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    entry_order = next(order for order in container.store.list_orders() if order.purpose == "ENTRY")
    entry_payload = json.loads(entry_order.payload_json)
    gateway, kill = _gateway(container)
    kill.activate(actor_id="test")
    risk_increasing = OrderIntent(
        symbol=position["symbol"],
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=None,
        stop_price=Decimal("1.00"),
        take_profits=(),
        leverage=1.0,
        decision_id=str(entry_order.decision_id or ""),
        risk_verdict_id=entry_order.risk_verdict_id,
        approval_id="APR-TEST",
        idempotency_key="risk-increasing-blocked",
        position_side=PositionSide.LONG,
        risk_amount_usd=Decimal("1.00"),
        risk_pct=0.1,
        purpose="entry",
        reduce_only=False,
    )

    with pytest.raises(KillSwitchActive):
        gateway.submit(
            risk_increasing,
            AuthContext(actor_id="cli", approval_id=entry_payload["approval_id"]),
            mark_price=Decimal("70000"),
            mark_as_of=utc_timestamp(NOW),
        )

    assert len(container.store.list_orders()) == 1


def test_a_missing_exit_verdict_is_rejected_before_the_broker(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    gateway, _kill = _gateway(container)
    intent = _exit_intent(
        position,
        idempotency_key="missing-verdict",
        risk_verdict_id="RISK-MISSING",
        decision_id="DEC-MISSING",
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=intent.approval_id),
            mark_price=Decimal(str(position["stop_price"])),
            mark_as_of=utc_timestamp(NOW),
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert len(container.store.list_orders()) == 1


def test_an_expired_exit_verdict_is_rejected(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    decision_id = "DEC-EXIT-EXPIRED"
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="POSITION_MANAGEMENT",
            decision_type="POSITION_MANAGEMENT",
            validation_status="VALID",
            raw_output_hash="expired",
            persisted_at=utc_timestamp(NOW - timedelta(seconds=60)),
            payload_json="{}",
        )
    )
    payload = {
        "verdict": "ALLOW",
        "requested": {
            "entry_price": str(position["mark_price"]),
            "stop_price": str(position["stop_price"]),
            "quantity": str(position["size"]),
            "risk_pct": 0.0,
            "leverage": float(position["leverage"]),
            "notional_usd": "1.00",
        },
        "allowed": {
            "quantity": str(position["size"]),
            "notional_usd": "1.00",
            "leverage": float(position["leverage"]),
            "risk_amount_usd": "0",
        },
        "binding_cap": "NONE",
        "reasons": [],
        "computed_at": utc_timestamp(NOW - timedelta(seconds=31)),
        "expires_at": utc_timestamp(NOW - timedelta(seconds=1)),
        "inputs_hash": "expired",
        "stale_inputs": [],
    }
    container.store.save_risk_verdict(
        RiskVerdictRecord(
            risk_verdict_id="RISK-EXPIRED",
            trigger_id=position["source_trigger_id"],
            decision_id=decision_id,
            verdict="ALLOW",
            binding_cap="NONE",
            computed_at=payload["computed_at"],
            expires_at=payload["expires_at"],
            payload_json=json.dumps(payload),
        )
    )
    gateway, _kill = _gateway(container)
    intent = _exit_intent(
        position,
        idempotency_key="expired-verdict",
        risk_verdict_id="RISK-EXPIRED",
        decision_id=decision_id,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=intent.approval_id),
            mark_price=Decimal(str(position["stop_price"])),
            mark_as_of=utc_timestamp(NOW),
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert "expired" in excinfo.value.message
    assert len(container.store.list_orders()) == 1


def test_a_position_without_a_source_trigger_fails_closed(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    _touch_stop(setup)
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["source_trigger_id"] = None
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()

    with pytest.raises(PositionCloseError) as excinfo:
        setup["service"].monitor(position_id)

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert len(container.store.list_orders()) == 1
    assert container.store.list_trade_logs(limit=None) == ()


def test_save_close_is_atomic_when_the_position_is_no_longer_open(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    _close(setup)
    closed_record = container.store.get_position(position_id)
    trade = container.store.get_trade_log_for_position(position_id)
    account = container.store.get_paper_account("ACC-PAPER")
    exit_order = container.store.get_order(trade.order_id)
    assert closed_record is not None and trade is not None
    assert account is not None and exit_order is not None
    now = utc_timestamp(NOW)
    rollback = ClosedPositionRecord(
        order=OrderRecord(
            order_id="ORD-ROLLBACK",
            client_order_id="client-rollback",
            environment="PAPER",
            symbol=position["symbol"],
            status="FILLED",
            risk_verdict_id=exit_order.risk_verdict_id,
            idempotency_key="idem-rollback",
            created_at=now,
            updated_at=now,
            payload_json="{}",
            trigger_id=position["source_trigger_id"],
            decision_id=trade.decision_id,
            purpose="EXIT",
        ),
        fill=FillRecord(
            fill_id="FIL-ROLLBACK",
            order_id="ORD-ROLLBACK",
            environment="PAPER",
            symbol=position["symbol"],
            filled_at=now,
            payload_json="{}",
        ),
        position=closed_record,
        account=PaperAccountRecord(
            account_id=account.account_id,
            environment=account.environment,
            currency=account.currency,
            initial_balance=account.initial_balance,
            balance=account.balance,
            updated_at=now,
        ),
        balance_delta="1.00",
        event=PositionEventRecord(
            position_id=position_id,
            seq=0,
            at=now,
            action="FULL_CLOSE",
            order_id="ORD-ROLLBACK",
            note="rollback attempt",
        ),
        trade_log=TradeLogRecord(
            trade_id="TRD-ROLLBACK",
            position_id=position_id,
            account_id=account.account_id,
            environment="PAPER",
            symbol=position["symbol"],
            side=position["side"],
            opened_at=position["opened_at"],
            closed_at=now,
            net_pnl_usd="0",
            idempotency_key="idem-rollback",
            payload_json="{}",
            order_id="ORD-ROLLBACK",
            fill_id="FIL-ROLLBACK",
            risk_verdict_id=exit_order.risk_verdict_id,
            decision_id=trade.decision_id,
        ),
    )

    with pytest.raises(OmsConflictError):
        container.store.save_close(rollback)

    # Nothing from the failed unit of work was committed.
    assert container.store.get_order("ORD-ROLLBACK") is None
    assert container.store.get_fill_by_order_id("ORD-ROLLBACK") is None
    assert container.store.get_trade_log("TRD-ROLLBACK") is None
    assert container.store.get_trade_log_for_position(position_id) == trade
    after = container.store.get_paper_account("ACC-PAPER")
    assert after is not None and after.balance == account.balance


def test_trade_log_read_models_filter_and_handle_missing_rows(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    _close(setup)

    logs = container.store.list_trade_logs(symbol="BTCUSDT", limit=10)
    assert len(logs) == 1
    assert container.store.count_trade_logs(symbol="BTCUSDT") == 1
    assert container.store.count_trade_logs(symbol="ETHUSDT") == 0
    assert container.store.list_trade_logs(symbol="ETHUSDT") == ()
    assert container.store.get_trade_log("TRD-MISSING") is None
    events = container.store.list_position_events(position["position_id"])
    assert len(events) == 1
    assert container.store.list_position_events("POS-MISSING") == ()
