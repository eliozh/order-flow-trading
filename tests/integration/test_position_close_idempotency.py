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
    classify_intent,
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
from position_support import set_position_mark

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


def _touch_stop(setup: dict[str, Any]) -> None:
    position = setup["position"]
    set_position_mark(
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
    symbol: str | None = None,
    position_id: str | None = None,
    position_side: PositionSide | None = None,
) -> OrderIntent:
    side = position_side or PositionSide(str(position["side"]))
    return OrderIntent(
        symbol=symbol or position["symbol"],
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
        position_id=position["position_id"] if position_id is None else position_id,
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


@pytest.mark.parametrize("component", ["order", "fill"])
def test_a_corrupt_replayed_exit_payload_is_data_unavailable(
    tmp_path: Path, component: str
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    first = _close(setup)
    assert first.idempotent_replay is False
    orders_before = container.store.count_orders()
    fills_before = container.store.count_fills()
    trades_before = container.store.count_trade_logs()
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        if component == "order":
            connection.execute(
                "UPDATE orders SET payload_json = ? WHERE purpose = 'EXIT'",
                ("not json",),
            )
        else:
            connection.execute(
                "UPDATE fills SET payload_json = ? WHERE order_id IN "
                "(SELECT order_id FROM orders WHERE purpose = 'EXIT')",
                ("not json",),
            )
        connection.commit()

    with pytest.raises(PositionCloseError) as excinfo:
        setup["service"].monitor(position_id)

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert component in excinfo.value.message
    # The replay degraded loudly; it created nothing.
    assert container.store.count_orders() == orders_before
    assert container.store.count_fills() == fills_before
    assert container.store.count_trade_logs() == trades_before


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
    # The blocked intent's own classification is audited before the switch
    # decision, so a killed risk-increasing intent is never invisible.
    classified = [
        json.loads(event.payload_json)
        for event in container.store.list_audit_events(limit=100)
        if event.event_type == "INTENT_CLASSIFIED"
    ]
    blocked = [
        entry
        for entry in classified
        if (entry.get("subject") or {}).get("id") == "risk-increasing-blocked"
    ]
    assert blocked
    assert blocked[0]["payload"]["risk_reducing"] is False
    assert blocked[0]["payload"]["purpose"] == "ENTRY"


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


def test_save_close_rolls_back_a_successful_flip_when_the_trade_log_conflicts(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    account = container.store.get_paper_account("ACC-PAPER")
    entry_order = next(order for order in container.store.list_orders() if order.purpose == "ENTRY")
    entry_fill = container.store.get_fill_by_order_id(entry_order.order_id)
    assert account is not None and entry_fill is not None
    assert entry_order.decision_id is not None
    now = utc_timestamp(NOW)
    # Seed an OPEN position with a conflicting trade-log row for the same
    # position. The close flips the position successfully, then the unique
    # trade-log insert must roll the whole unit back.
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_logs (trade_id, position_id, account_id, environment, symbol, "
            "side, opened_at, closed_at, net_pnl_usd, idempotency_key, payload_json, order_id, "
            "fill_id, risk_verdict_id, decision_id) "
            "VALUES ('TRD-CONFLICT', ?, ?, 'PAPER', ?, ?, ?, ?, '0', 'idem-conflict', '{}', ?, "
            "?, ?, ?)",
            (
                position_id,
                account.account_id,
                position["symbol"],
                position["side"],
                position["opened_at"],
                now,
                entry_order.order_id,
                entry_fill.fill_id,
                entry_order.risk_verdict_id,
                entry_order.decision_id,
            ),
        )
        connection.commit()
    open_record = container.store.get_position(position_id)
    assert open_record is not None and open_record.state == "OPEN"
    rollback = ClosedPositionRecord(
        order=OrderRecord(
            order_id="ORD-ROLLBACK",
            client_order_id="client-rollback",
            environment="PAPER",
            symbol=position["symbol"],
            status="FILLED",
            risk_verdict_id=entry_order.risk_verdict_id,
            idempotency_key="idem-rollback",
            created_at=now,
            updated_at=now,
            payload_json="{}",
            trigger_id=position["source_trigger_id"],
            decision_id=entry_order.decision_id,
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
        position=open_record,
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
            seq=None,
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
            net_pnl_usd="0.00000000",
            idempotency_key="idem-rollback",
            payload_json="{}",
            order_id="ORD-ROLLBACK",
            fill_id="FIL-ROLLBACK",
            risk_verdict_id=entry_order.risk_verdict_id,
            decision_id=entry_order.decision_id,
        ),
    )

    with pytest.raises(OmsConflictError):
        container.store.save_close(rollback)

    # The position flip that happened earlier in the transaction was rolled
    # back: the row is still OPEN and nothing from the unit of work survives.
    after_position = container.store.get_position(position_id)
    assert after_position is not None and after_position.state == "OPEN"
    assert container.store.get_order("ORD-ROLLBACK") is None
    assert container.store.get_fill_by_order_id("ORD-ROLLBACK") is None
    assert container.store.get_trade_log("TRD-ROLLBACK") is None
    assert container.store.list_position_events(position_id) == ()
    after_account = container.store.get_paper_account("ACC-PAPER")
    assert after_account is not None and after_account.balance == account.balance


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


def _seed_exit_verdict(
    container: Any,
    position: dict[str, Any],
    *,
    decision_id: str,
    risk_verdict_id: str = "RISK-EXIT",
    trigger_id: str | None = None,
    allowed_quantity: str | None = None,
) -> None:
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="POSITION_MANAGEMENT",
            decision_type="POSITION_MANAGEMENT",
            validation_status="VALID",
            raw_output_hash="test",
            persisted_at=utc_timestamp(NOW),
            payload_json="{}",
        )
    )
    expires_at = utc_timestamp(NOW + timedelta(seconds=30))
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
            "quantity": allowed_quantity or str(position["size"]),
            "notional_usd": "1.00",
            "leverage": float(position["leverage"]),
            "risk_amount_usd": "0",
        },
        "binding_cap": "NONE",
        "reasons": [],
        "computed_at": utc_timestamp(NOW),
        "expires_at": expires_at,
        "inputs_hash": "test",
        "stale_inputs": [],
    }
    container.store.save_risk_verdict(
        RiskVerdictRecord(
            risk_verdict_id=risk_verdict_id,
            trigger_id=trigger_id or position["source_trigger_id"],
            decision_id=decision_id,
            verdict="ALLOW",
            binding_cap="NONE",
            computed_at=utc_timestamp(NOW),
            expires_at=expires_at,
            payload_json=json.dumps(payload),
        )
    )


def _seed_other_trigger(container: Any) -> str:
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES ('OPP-OTHER', 'BTCUSDT', 'TRIGGERED', 't', 't', '{}', NULL)"
        )
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES ('TRG-OTHER', 'OPP-OTHER', 'BTCUSDT', 'PENDING_RECHECK', "
            "'TRG-OTHER-KEY', 't', '{}')"
        )
        connection.commit()
    return "TRG-OTHER"


@pytest.mark.parametrize(
    "case",
    [
        "decision_mismatch",
        "quantity_cap",
        "position_missing",
        "position_not_open",
        "symbol_mismatch",
        "side_mismatch",
        "trigger_mismatch",
        "mark_mismatch",
        "missing_mark",
    ],
)
def test_each_exit_binding_guard_is_typed_and_creates_no_order(tmp_path: Path, case: str) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))
    verdict_decision = "DEC-EXIT-OK"
    intent_decision = "DEC-EXIT-OK"
    verdict_trigger = position["source_trigger_id"]
    allowed_quantity: str | None = None
    intent_overrides: dict[str, Any] = {}
    mark = Decimal(str(position["stop_price"]))

    if case == "decision_mismatch":
        verdict_decision = "DEC-VERDICT-OTHER"
    elif case == "quantity_cap":
        allowed_quantity = str(Decimal(position["size"]) / Decimal("2"))
    elif case == "position_missing":
        intent_overrides["position_id"] = "POS-MISSING"
    elif case == "position_not_open":
        _close(setup)
    elif case == "symbol_mismatch":
        intent_overrides["symbol"] = "ETHUSDT"
    elif case == "side_mismatch":
        intent_overrides["position_side"] = (
            PositionSide.SHORT if position["side"] == "LONG" else PositionSide.LONG
        )
    elif case == "trigger_mismatch":
        verdict_trigger = _seed_other_trigger(container)
    elif case == "mark_mismatch":
        mark = mark + Decimal("1")
    elif case == "missing_mark":
        set_position_mark(container, position_id, None, as_of=utc_timestamp(NOW))

    _seed_exit_verdict(
        container,
        position,
        decision_id=verdict_decision,
        trigger_id=verdict_trigger,
        allowed_quantity=allowed_quantity,
    )
    before = container.store.count_orders()
    gateway, _kill = _gateway(container)
    intent = _exit_intent(
        position,
        idempotency_key=f"guard-{case}",
        risk_verdict_id="RISK-EXIT",
        decision_id=intent_decision,
        **intent_overrides,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=intent.approval_id),
            mark_price=mark,
            mark_as_of=utc_timestamp(NOW),
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.count_orders() == before


def _bare_intent(**overrides: Any) -> OrderIntent:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.01"),
        "price": None,
        "stop_price": Decimal("1.00"),
        "take_profits": (),
        "leverage": 1.0,
        "decision_id": "DEC-BARE",
        "risk_verdict_id": "RISK-BARE",
        "approval_id": "APR-BARE",
        "idempotency_key": "bare-intent",
        "position_side": PositionSide.LONG,
        "risk_amount_usd": Decimal("1.00"),
        "risk_pct": 0.1,
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_purpose_normalization_derives_reduce_only_from_the_one_representation() -> None:
    entry_with_reduce_only = _bare_intent(purpose="entry", reduce_only=True)
    assert entry_with_reduce_only.purpose == "ENTRY"
    assert entry_with_reduce_only.reduce_only is False
    assert classify_intent(entry_with_reduce_only).risk_reducing is False

    exit_without_reduce_only = _bare_intent(purpose="exit", reduce_only=False)
    assert exit_without_reduce_only.purpose == "EXIT"
    assert exit_without_reduce_only.reduce_only is True
    assert classify_intent(exit_without_reduce_only).risk_reducing is True

    uppercase_exit = _bare_intent(purpose="EXIT", reduce_only=True)
    assert uppercase_exit.purpose == "EXIT"
    assert uppercase_exit.reduce_only is True
    assert classify_intent(uppercase_exit).risk_reducing is True


def test_a_non_exit_intent_is_never_treated_as_a_protective_exit(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    _seed_exit_verdict(
        container,
        position,
        decision_id="DEC-NON-EXIT",
        trigger_id=position["source_trigger_id"],
    )
    before = container.store.count_orders()
    gateway, _kill = _gateway(container)
    intent = _bare_intent(
        decision_id="DEC-NON-EXIT",
        risk_verdict_id="RISK-EXIT",
        idempotency_key="non-exit-intent",
        purpose="entry",
        reduce_only=True,
    )

    with pytest.raises(GatewayError) as excinfo:
        gateway.submit(
            intent,
            AuthContext(actor_id="cli", approval_id=intent.approval_id),
            mark_price=Decimal("70000"),
            mark_as_of=utc_timestamp(NOW),
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.count_orders() == before
