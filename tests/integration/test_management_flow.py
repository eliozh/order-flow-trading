"""Integration: validated management decisions execute through the gateway in paper."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import oft.application.management_service as management_service_module
from oft.adapters.llm_stub import LLMStub
from oft.application import management_add
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    ManagementRefusal,
    ManagementRejected,
    ManagementService,
)
from oft.application.position_service import PositionCloseError, PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.positions import position_from_contract
from oft.ports.llm import LLMRole

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _setup(
    tmp_path: Path,
    *,
    management_fixture: str = "management_hold.json",
) -> dict[str, Any]:
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
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="management-entry")
    assert accepted.position is not None
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: management_fixture},
    )
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
        runtime=container.llm_runtime,
    )
    return {
        "container": container,
        "settings": settings,
        "position": accepted.position,
        "order": accepted.order,
        "kill": kill,
        "service": service,
    }


def _position_payload(container: Any, position_id: str) -> dict[str, Any]:
    record = container.store.get_position(position_id)
    assert record is not None
    return json.loads(record.payload_json)


def _orders_by_purpose(container: Any, purpose: str) -> list[Any]:
    return [
        record for record in container.store.list_orders(limit=1000) if record.purpose == purpose
    ]


def _rewrite_position_payload(
    settings: Settings, position_id: str, payload: dict[str, Any]
) -> None:
    """Overwrite one stored position payload (to stage a state under test)."""
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def _stop_update_count(container: Any) -> int:
    return sum(
        1
        for event in container.store.list_audit_events_ascending()
        if event.event_type == "STOP_UPDATED"
    )


def _audit_payloads(container: Any, event_type: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in container.store.list_audit_events_ascending():
        if event.event_type != event_type:
            continue
        body = json.loads(event.payload_json)
        payload = body.get("payload") if isinstance(body, dict) else None
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def test_hold_creates_no_order_and_persists_the_action_and_provenance(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-hold")

    assert result.status == "NO_ACTION"
    assert result.action == "HOLD"
    assert result.executed is False
    assert result.order is None
    assert container.store.count_orders() == orders_before
    payload = _position_payload(container, position_id)
    history = payload["management_history"]
    assert history[-1]["action"] == "HOLD"
    assert history[-1]["decision_id"] == result.decision_id
    assert history[-1]["executed_order_id"] is None
    decision = container.store.get_decision(result.decision_id)
    assert decision is not None and decision.role == "MANAGEMENT"
    envelope = json.loads(decision.payload_json)
    provenance = envelope["provenance"]
    assert provenance["provider"] == "stub"
    assert provenance["model"] == "fixture"
    assert provenance["prompt_version"] >= 1
    assert provenance["prompt_hash"]
    assert provenance["cost"]["amount_usd"] == "0"
    assert result.llm is not None
    assert result.llm["provenance"]["provider"] == "stub"


def test_partial_close_reduces_the_open_position_and_writes_no_trade_log(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    size = Decimal(position["size"])

    result = setup["service"].manage(position_id, idempotency_key="mgmt-partial")

    assert result.status == "EXECUTED"
    assert result.action == "PARTIAL_CLOSE"
    assert result.executed is True
    payload = _position_payload(container, position_id)
    assert payload["state"] == "OPEN"
    assert Decimal(payload["size"]) < size
    assert Decimal(payload["realized_pnl_usd"]) != 0
    assert container.store.list_trade_logs(limit=None) == ()
    assert container.store.count_positions() == 1
    orders = _orders_by_purpose(container, "PARTIAL_CLOSE")
    assert len(orders) == 1
    order = json.loads(orders[0].payload_json)
    assert order["reduce_only"] is True
    assert order["client_order_id"].startswith("oft-paper-BTCUSDT-partial_close-")
    assert order["risk_verdict_id"]
    assert [event.action for event in container.store.list_position_events(position_id)] == [
        "PARTIAL_CLOSE"
    ]


def test_a_partial_close_remainder_below_min_notional_escalates_to_full_close(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE symbols SET min_notional = '10000.00' WHERE symbol = 'BTCUSDT'")
        connection.commit()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-escalate")

    assert result.action == "FULL_CLOSE"
    assert result.status == "EXECUTED"
    assert result.reason is not None and "remainder" in result.reason
    payload = _position_payload(container, position_id)
    assert payload["state"] == "CLOSED"
    assert "remainder" in payload["management_history"][-1]["note"]
    assert len(container.store.list_trade_logs(limit=None)) == 1


def test_full_close_creates_exactly_one_trade_log(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_full_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-full")

    assert result.status == "EXECUTED"
    assert result.action == "FULL_CLOSE"
    payload = _position_payload(container, position_id)
    assert payload["state"] == "CLOSED"
    trades = container.store.list_trade_logs(limit=None)
    assert len(trades) == 1
    trade = json.loads(trades[0].payload_json)
    assert "MANUAL_CLOSE" in trade["outcome_tags"]
    assert "STOPPED" not in trade["outcome_tags"]
    assert result.trade_log is not None
    assert result.trade_log["trade_id"] == trade["trade_id"]
    assert [event.action for event in container.store.list_position_events(position_id)] == [
        "FULL_CLOSE"
    ]


def test_add_is_clamped_to_the_stricter_cap_and_records_requested_and_allowed(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    size = Decimal(position["size"])

    result = setup["service"].manage(position_id, idempotency_key="mgmt-add")

    assert result.status == "EXECUTED"
    assert result.action == "ADD"
    assert result.risk is not None
    requested = Decimal(result.risk["requested"]["quantity"])
    allowed = Decimal(result.risk["allowed"]["quantity"])
    assert requested > allowed > 0
    # Exactly the binding cap/value for this fixture and account, not just a
    # membership check: 20% of 9800 equity at the 70002 mark binds MAX_NOTIONAL.
    assert result.risk["binding_cap"] == "MAX_NOTIONAL"
    assert requested == Decimal("0.03991468169580838323353293413")
    assert allowed == Decimal("0.028")
    payload = _position_payload(container, position_id)
    assert Decimal(payload["size"]) == size + allowed
    assert container.store.count_positions() == 1
    assert container.store.list_trade_logs(limit=None) == ()
    orders = _orders_by_purpose(container, "ADD")
    assert len(orders) == 1
    order = json.loads(orders[0].payload_json)
    assert order["reduce_only"] is False
    assert order["client_order_id"].startswith("oft-paper-BTCUSDT-add-")
    assert Decimal(order["quantity"]) == allowed
    verdict = container.store.get_risk_verdict(order["risk_verdict_id"])
    assert verdict is not None and verdict.verdict == "ALLOW"


def test_an_oversized_add_proposal_is_clamped_not_trusted(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_oversized_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-oversized")

    assert result.status == "EXECUTED"
    assert result.risk is not None
    requested = Decimal(result.risk["requested"]["quantity"])
    allowed = Decimal(result.risk["allowed"]["quantity"])
    assert requested > allowed
    order = json.loads(_orders_by_purpose(container, "ADD")[0].payload_json)
    assert Decimal(order["quantity"]) == allowed


def test_an_add_is_blocked_when_a_cap_is_reached_and_creates_no_order(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=NOW.isoformat(),
    )
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position["position_id"], idempotency_key="mgmt-add-blocked")

    assert result.status == "BLOCKED"
    assert result.executed is False
    assert result.order is None
    assert container.store.count_orders() == orders_before
    assert result.risk is not None
    assert result.risk["binding_cap"] == "MAX_POSITIONS"


def test_a_tightened_stop_is_applied_and_audited(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-tighten")

    assert result.status == "EXECUTED"
    payload = _position_payload(container, position_id)
    assert payload["stop_price"] == "69500.00"
    events = [
        event.event_type
        for event in container.store.list_audit_events_ascending()
        if event.event_type in {"STOP_UPDATED", "MANAGEMENT_CLASSIFIED"}
    ]
    assert "STOP_UPDATED" in events


def test_an_invalid_stop_side_is_rejected_without_an_order(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_invalid_stop.json")
    container = setup["container"]
    position = setup["position"]
    orders_before = container.store.count_orders()

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key="mgmt-invalid-stop")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    assert excinfo.value.field == "position_management.new_stop_price"
    assert container.store.count_orders() == orders_before
    assert container.store.list_position_events(position["position_id"]) == ()


def test_every_management_order_has_a_verdict_classification_and_audit(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]

    result = setup["service"].manage(position["position_id"], idempotency_key="mgmt-audit")

    assert result.order is not None
    assert result.order["risk_verdict_id"]
    events = {
        event.event_type
        for event in container.store.list_audit_events_ascending()
        if event.event_type in {"INTENT_CLASSIFIED", "RISK_VERDICT", "POSITION_PARTIAL_CLOSE"}
    }
    assert {"INTENT_CLASSIFIED", "RISK_VERDICT", "POSITION_PARTIAL_CLOSE"} <= events


def test_an_add_is_blocked_when_the_position_leverage_exceeds_the_cap(
    tmp_path: Path,
) -> None:
    """H1: a tightened leverage cap blocks the add whichever quantity cap binds."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    assert position["leverage"] == 2.0
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_leverage": 1.0}),
        updated_at=NOW.isoformat(),
    )
    orders_before = container.store.count_orders()

    result = setup["service"].manage(
        position["position_id"], idempotency_key="mgmt-add-over-leverage"
    )

    assert result.status == "BLOCKED"
    assert result.executed is False
    assert result.order is None
    assert container.store.count_orders() == orders_before
    assert _orders_by_purpose(container, "ADD") == []
    assert result.risk is not None
    assert result.risk["binding_cap"] == "MAX_LEVERAGE"


def test_a_profit_locking_stop_between_entry_and_mark_is_applied(tmp_path: Path) -> None:
    """H2: validation and persistence use the same recorded-mark reference."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    payload = _position_payload(container, position_id)
    payload["entry_price"] = "65000.00"
    payload["avg_entry_price"] = "65000.00"
    _rewrite_position_payload(setup["settings"], position_id, payload)
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-profit-stop")

    assert result.status == "EXECUTED"
    assert result.action == "PARTIAL_CLOSE"
    after = _position_payload(container, position_id)
    assert after["stop_price"] == "69500.00"
    assert container.store.count_orders() == orders_before + 1
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1


def test_a_hold_does_not_refresh_the_stored_mark_as_of(tmp_path: Path) -> None:
    """H4: an order-less change must not make the old stored price look fresh."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    staged = _position_payload(container, position_id)
    old_as_of = "2026-10-06T00:00:00.000Z"
    staged["mark_price"] = "69000.00"
    staged["updated_at"] = old_as_of
    _rewrite_position_payload(setup["settings"], position_id, staged)

    service = PositionService(
        store=container.store, clock=container.clock, settings=setup["settings"]
    )
    service.record_management_hold(
        position_id,
        decision_id="MGD-TEST-HOLD",
        note="hold",
        now="2026-10-08T00:00:00.000Z",
    )

    after = _position_payload(container, position_id)
    assert after["updated_at"] == old_as_of
    assert after["mark_price"] == "69000.00"
    observation = service._resolve_mark(
        position_from_contract(after),
        after,
        datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert observation is not None
    assert observation.source == "BARS"
    assert observation.mark != Decimal("69000.00")


def test_two_decisions_persist_two_distinct_add_verdict_rows(tmp_path: Path) -> None:
    """M1: the verdict id includes the decision id, so rows never overwrite."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=NOW.isoformat(),
    )

    first = setup["service"].manage(position["position_id"], idempotency_key="blocked-a")
    second = setup["service"].manage(position["position_id"], idempotency_key="blocked-b")

    assert first.status == "BLOCKED" and second.status == "BLOCKED"
    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT decision_id FROM risk_verdicts WHERE risk_verdict_id LIKE 'RISK-MGD-%'"
        ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {first.decision_id, second.decision_id}


def test_the_execution_reevaluation_cannot_be_forced_to_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a disagreeing execution verdict refuses instead of rewriting ALLOW."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    orders_before = container.store.count_orders()
    real_evaluate = management_add.evaluate_risk
    state = {"calls": 0}

    def fake_evaluate(**kwargs: Any) -> Any:
        state["calls"] += 1
        if state["calls"] == 2:
            kwargs["request"] = replace(kwargs["request"], stop_price=None)
        return real_evaluate(**kwargs)

    monkeypatch.setattr(management_add, "evaluate_risk", fake_evaluate)

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key="mgmt-gate-disagree")

    assert excinfo.value.code == "RISK_BLOCKED"
    assert container.store.count_orders() == orders_before
    assert _orders_by_purpose(container, "ADD") == []


def _lose_stop_transaction(monkeypatch: pytest.MonkeyPatch, setup: dict[str, Any]) -> None:
    """Make the next stop/target write a no-op, modelling a lost second commit.

    The order, fill, and history row still commit, but the separate stop
    transaction does not: exactly the crash gap a same-key replay must repair.
    """

    positions = setup["service"]._positions

    def lost(position_id: str, **_kwargs: Any) -> dict[str, Any]:
        return _position_payload(setup["container"], position_id)

    monkeypatch.setattr(positions, "apply_management_stop", lost)


def test_a_replay_reapplies_a_stop_change_lost_after_the_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: a same-key replay reconciles a stop the second transaction lost."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    positions = setup["service"]._positions
    real_apply = positions.apply_management_stop

    _lose_stop_transaction(monkeypatch, setup)
    first = setup["service"].manage(position_id, idempotency_key="mgmt-replay-stop")
    assert first.status == "EXECUTED"
    # The stop transaction was lost: the row still carries the pre-change stop
    # and no STOP_UPDATED audit exists, so the change is provably unapplied.
    assert _position_payload(container, position_id)["stop_price"] == "69000.00"

    def stop_updates() -> int:
        return sum(
            1
            for event in container.store.list_audit_events_ascending()
            if event.event_type == "STOP_UPDATED"
        )

    assert stop_updates() == 0
    monkeypatch.setattr(positions, "apply_management_stop", real_apply)
    replay = setup["service"].manage(position_id, idempotency_key="mgmt-replay-stop")
    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "69500.00"
    assert stop_updates() == 1
    setup["service"].manage(position_id, idempotency_key="mgmt-replay-stop")
    assert stop_updates() == 1


def test_an_invalid_persisted_cap_is_a_typed_management_rejection(tmp_path: Path) -> None:
    """M6: a caps error is translated to a typed management code, not a 500."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_leverage": 0}),
        updated_at=NOW.isoformat(),
    )
    orders_before = container.store.count_orders()

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key="mgmt-bad-caps")

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert container.store.count_orders() == orders_before


def test_a_partial_close_persists_non_empty_new_targets(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_targets.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-targets")

    assert result.status == "EXECUTED"
    assert result.action == "PARTIAL_CLOSE"
    assert _position_payload(container, position_id)["target_prices"] == [
        "71000.00",
        "72000.00",
    ]
    assert any(
        event.event_type == "STOP_UPDATED"
        for event in container.store.list_audit_events_ascending()
    )


def test_a_target_only_update_on_a_stopless_long_position_keeps_the_stop_null(
    tmp_path: Path,
) -> None:
    """R1: a target-only change must never synthesize a zero stop."""
    setup = _setup(tmp_path, management_fixture="management_partial_targets.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    staged = _position_payload(container, position_id)
    staged["stop_price"] = None
    _rewrite_position_payload(setup["settings"], position_id, staged)

    result = setup["service"].manage(position_id, idempotency_key="mgmt-stopless-targets")

    assert result.status == "EXECUTED"
    after = _position_payload(container, position_id)
    assert after["stop_price"] is None
    assert after["target_prices"] == ["71000.00", "72000.00"]


def test_a_target_only_update_on_a_stopless_short_position_does_not_raise(
    tmp_path: Path,
) -> None:
    """R1: the stop-less target-only path must also accept a SHORT position."""
    setup = _setup(tmp_path, management_fixture="management_partial_targets_short.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    staged = _position_payload(container, position_id)
    staged["side"] = "SHORT"
    staged["stop_price"] = None
    staged["target_prices"] = []
    _rewrite_position_payload(setup["settings"], position_id, staged)

    result = setup["service"].manage(position_id, idempotency_key="mgmt-short-targets")

    assert result.status == "EXECUTED"
    after = _position_payload(container, position_id)
    assert after["stop_price"] is None
    assert after["target_prices"] == ["69000.00", "68000.00"]


def test_a_stale_key_replay_does_not_roll_back_a_later_stop_change(
    tmp_path: Path,
) -> None:
    """R2: a stale idempotency key must never loosen a newer stop."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    first = setup["service"].manage(position_id, idempotency_key="mgmt-stale-key")
    assert first.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "69500.00"

    staged = _position_payload(container, position_id)
    staged["stop_price"] = "70000.00"
    staged["management_history"] = [
        *staged["management_history"],
        {
            "decision_id": "MGD-LATER-ACTION",
            "action": "PARTIAL_CLOSE",
            "at": "2026-10-08T01:00:00.000Z",
            "executed_order_id": "ORD-LATER",
            "note": "a later management action",
        },
    ]
    _rewrite_position_payload(setup["settings"], position_id, staged)
    updates_before = _stop_update_count(container)

    replay = setup["service"].manage(position_id, idempotency_key="mgmt-stale-key")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    after = _position_payload(container, position_id)
    assert after["stop_price"] == "70000.00"
    assert _stop_update_count(container) == updates_before


def test_a_replay_does_not_overwrite_a_stop_changed_outside_the_history(
    tmp_path: Path,
) -> None:
    """R2: the pre-change snapshot also guards a change with no history entry."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    setup["service"].manage(position_id, idempotency_key="mgmt-snapshot-guard")

    staged = _position_payload(container, position_id)
    staged["stop_price"] = "70000.00"
    _rewrite_position_payload(setup["settings"], position_id, staged)
    updates_before = _stop_update_count(container)

    replay = setup["service"].manage(position_id, idempotency_key="mgmt-snapshot-guard")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "70000.00"
    assert _stop_update_count(container) == updates_before


def test_a_crash_gap_replay_reapplies_the_stop_once_at_the_decision_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2/db-N1: recovery validates against the decision mark, not the fill mark."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    positions = setup["service"]._positions
    real_apply = positions.apply_management_stop

    _lose_stop_transaction(monkeypatch, setup)
    first = setup["service"].manage(position_id, idempotency_key="mgmt-crash-gap")
    assert first.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "69000.00"

    staged = _position_payload(container, position_id)
    # A post-fill stored mark below the recovered stop: validating against it
    # would reject the stop, while the decision mark (70002) accepts it.
    staged["mark_price"] = "69400.00"
    _rewrite_position_payload(setup["settings"], position_id, staged)
    updates_before = _stop_update_count(container)

    monkeypatch.setattr(positions, "apply_management_stop", real_apply)
    replay = setup["service"].manage(position_id, idempotency_key="mgmt-crash-gap")

    assert replay.idempotent_replay is True
    assert _position_payload(container, position_id)["stop_price"] == "69500.00"
    assert _stop_update_count(container) == updates_before + 1

    setup["service"].manage(position_id, idempotency_key="mgmt-crash-gap")
    assert _stop_update_count(container) == updates_before + 1


def test_a_later_hold_does_not_mask_a_lost_stop_change_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: a later HOLD must not hide this decision's lost stop transaction.

    Reproduces the silent drop: PARTIAL_CLOSE commits order+fill+history, the
    separate stop transaction is lost, a later HOLD appends history, and the
    same-key replay used to return EXECUTED with the stale stop unchanged.
    """
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    positions = setup["service"]._positions
    real_apply = positions.apply_management_stop

    _lose_stop_transaction(monkeypatch, setup)
    first = setup["service"].manage(position_id, idempotency_key="mgmt-lost-then-hold")
    assert first.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "69000.00"
    assert _stop_update_count(container) == 0

    monkeypatch.setattr(positions, "apply_management_stop", real_apply)
    positions.record_management_hold(
        position_id,
        decision_id="MGD-LATER-HOLD",
        note="a later hold",
        now="2026-10-08T01:00:00.000Z",
    )
    assert _position_payload(container, position_id)["management_history"][-1]["action"] == "HOLD"

    replay = setup["service"].manage(position_id, idempotency_key="mgmt-lost-then-hold")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert _position_payload(container, position_id)["stop_price"] == "69500.00"
    assert _stop_update_count(container) == 1
    reconciled = _audit_payloads(container, "MANAGEMENT_RECONCILED")
    assert reconciled and reconciled[-1]["decision_id"] == replay.decision_id

    # The change is now applied, so a further replay is a plain executed replay.
    setup["service"].manage(position_id, idempotency_key="mgmt-lost-then-hold")
    assert _position_payload(container, position_id)["stop_price"] == "69500.00"
    assert _stop_update_count(container) == 1


def test_an_unverifiable_lost_change_is_data_unavailable_with_a_skip_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: never EXECUTED with a dropped change when the snapshot no longer matches."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    positions = setup["service"]._positions
    real_apply = positions.apply_management_stop

    _lose_stop_transaction(monkeypatch, setup)
    first = setup["service"].manage(position_id, idempotency_key="mgmt-unverifiable")
    assert first.status == "EXECUTED"
    monkeypatch.setattr(positions, "apply_management_stop", real_apply)

    # A third party moved the stop away from the pre-change snapshot without an
    # applied audit for this decision: reconciliation cannot be proven safe.
    staged = _position_payload(container, position_id)
    staged["stop_price"] = "68500.00"
    _rewrite_position_payload(setup["settings"], position_id, staged)

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="mgmt-unverifiable")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    skipped = _audit_payloads(container, "MANAGEMENT_RECONCILE_SKIPPED")
    assert skipped
    assert skipped[-1]["decision_id"] == first.decision_id
    assert skipped[-1]["reason"]
    assert _position_payload(container, position_id)["stop_price"] == "68500.00"
    assert _stop_update_count(container) == 0


def test_a_later_non_hold_action_blocks_stop_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: a later non-HOLD action owns the row; reconciliation fails closed."""
    setup = _setup(tmp_path, management_fixture="management_tighten_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    positions = setup["service"]._positions
    real_apply = positions.apply_management_stop

    _lose_stop_transaction(monkeypatch, setup)
    first = setup["service"].manage(position_id, idempotency_key="mgmt-later-action")
    assert first.status == "EXECUTED"
    monkeypatch.setattr(positions, "apply_management_stop", real_apply)

    staged = _position_payload(container, position_id)
    staged["management_history"] = [
        *staged["management_history"],
        {
            "decision_id": "MGD-LATER-ACTION",
            "action": "PARTIAL_CLOSE",
            "at": "2026-10-08T01:00:00.000Z",
            "executed_order_id": "ORD-LATER",
            "note": "a later management action",
        },
    ]
    _rewrite_position_payload(setup["settings"], position_id, staged)

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="mgmt-later-action")

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    skipped = _audit_payloads(container, "MANAGEMENT_RECONCILE_SKIPPED")
    assert skipped and skipped[-1]["decision_id"] == first.decision_id
    assert _position_payload(container, position_id)["stop_price"] == "69000.00"
    assert _stop_update_count(container) == 0


def test_the_over_leverage_add_block_is_audited_with_the_effective_cap(
    tmp_path: Path,
) -> None:
    """C1: stored evidence names MAX_LEVERAGE, matching the returned result."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_leverage": 1.0}),
        updated_at=NOW.isoformat(),
    )

    result = setup["service"].manage(
        position["position_id"], idempotency_key="mgmt-audit-over-leverage"
    )

    assert result.status == "BLOCKED"
    assert result.risk is not None
    assert result.risk["binding_cap"] == "MAX_LEVERAGE"
    blocked = _audit_payloads(container, "MANAGEMENT_BLOCKED")
    assert blocked
    assert blocked[-1]["decision_id"] == result.decision_id
    assert blocked[-1]["binding_cap"] == "MAX_LEVERAGE"
    assert blocked[-1]["reason_code"] == "RISK_BLOCKED"


def test_a_semantic_rejection_pins_the_management_decision_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the semantic-rejection envelope must pin its decision type explicitly."""
    setup = _setup(tmp_path, management_fixture="management_invalid_stop.json")
    position = setup["position"]
    real_envelope = management_service_module.decision_envelope
    calls: list[dict[str, Any]] = []

    def recording_envelope(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return real_envelope(*args, **kwargs)

    monkeypatch.setattr(management_service_module, "decision_envelope", recording_envelope)

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key="mgmt-semantic-reject")

    assert excinfo.value.code == "LLM_SCHEMA_INVALID"
    rejected = [call for call in calls if call.get("validation_status") == "REJECTED"]
    assert rejected
    assert rejected[-1].get("decision_type") == "POSITION_MANAGEMENT"


def test_a_loosened_stop_is_refused_but_the_reduce_only_close_executes(
    tmp_path: Path,
) -> None:
    """C1: a loosened stop never applies; the partner partial close still runs."""
    setup = _setup(tmp_path, management_fixture="management_loosen_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-loosen")

    assert result.status == "EXECUTED"
    assert result.action == "PARTIAL_CLOSE"
    assert result.executed is True
    assert container.store.count_orders() == orders_before + 1
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1
    assert _orders_by_purpose(container, "FULL_CLOSE") == []
    assert _orders_by_purpose(container, "ADD") == []
    after = _position_payload(container, position_id)
    assert Decimal(after["size"]) < Decimal(position["size"])
    assert after["stop_price"] == position["stop_price"]
    assert [refusal.to_contract() for refusal in result.refusals] == [
        {
            "code": "RISK_BLOCKED",
            "field": "position_management.new_stop_price",
            "message": "a management action may not loosen or remove the protective stop",
        }
    ]
    assert isinstance(result.refusals[0], ManagementRefusal)
    assert result.reason is not None and "RISK_BLOCKED" in result.reason
    refused = _audit_payloads(container, "MANAGEMENT_STOP_REFUSED")
    assert refused
    assert refused[-1]["decision_id"] == result.decision_id
    assert refused[-1]["action"] == "PARTIAL_CLOSE"
    assert refused[-1]["code"] == "RISK_BLOCKED"
    assert refused[-1]["field"] == "position_management.new_stop_price"

    replay = setup["service"].manage(position_id, idempotency_key="mgmt-loosen")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert replay.refusals == result.refusals
    assert container.store.count_orders() == orders_before + 1
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1
    assert _position_payload(container, position_id)["stop_price"] == position["stop_price"]


def test_apply_management_stop_refuses_a_loosened_stop(tmp_path: Path) -> None:
    """C1 defense in depth: no direct/replay caller can loosen the stop."""
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    updates_before = _stop_update_count(container)
    service = PositionService(
        store=container.store, clock=container.clock, settings=setup["settings"]
    )

    with pytest.raises(PositionCloseError) as excinfo:
        service.apply_management_stop(
            position_id,
            stop_price=Decimal("68000.00"),
            target_prices=None,
            decision_id="MGD-LOOSEN-DIRECT",
            now="2026-10-08T00:00:00.000Z",
            reference=Decimal(_position_payload(container, position_id)["mark_price"]),
        )

    assert excinfo.value.code == "RISK_BLOCKED"
    assert _stop_update_count(container) == updates_before


def test_a_stopless_add_is_blocked_with_the_missing_stop_cap(tmp_path: Path) -> None:
    """M8: an ADD without a protective stop is a typed BLOCKED, never an order."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    staged = _position_payload(container, position_id)
    staged["stop_price"] = None
    _rewrite_position_payload(setup["settings"], position_id, staged)
    orders_before = container.store.count_orders()

    result = setup["service"].manage(position_id, idempotency_key="mgmt-stopless-add")

    assert result.status == "BLOCKED"
    assert result.executed is False
    assert result.risk is not None
    assert result.risk["binding_cap"] == "MISSING_STOP"
    assert container.store.count_orders() == orders_before
    assert _orders_by_purpose(container, "ADD") == []


def test_a_stale_mark_is_a_typed_refusal_without_an_order(tmp_path: Path) -> None:
    """M8: the management mark must be fresh at decision time."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    later = FixedClock(datetime(2026, 10, 20, tzinfo=UTC))
    service = ManagementService(
        store=container.store,
        clock=later,
        provider=LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
        settings=setup["settings"],
        kill_switch=KillSwitch(settings=setup["settings"], store=container.store, clock=later),
    )
    orders_before = container.store.count_orders()

    with pytest.raises(PositionCloseError) as excinfo:
        service.manage(position["position_id"], idempotency_key="mgmt-stale-mark")

    assert excinfo.value.code == "DATA_STALE"
    assert container.store.count_orders() == orders_before


class _AdvancingClock:
    """A clock whose provider call advances time, modelling a slow LLM call."""

    def __init__(self, start: datetime) -> None:
        self._instant = start

    def now(self) -> datetime:
        return self._instant

    def advance(self, seconds: int) -> None:
        self._instant = self._instant + timedelta(seconds=seconds)


class _AdvancingProvider(LLMStub):
    def __init__(self, clock: _AdvancingClock, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clock = clock

    def complete(self, request):  # type: ignore[no-untyped-def]
        self._clock.advance(120)
        return super().complete(request)


def test_the_clock_is_re_read_after_slow_orchestration(tmp_path: Path) -> None:
    """H1: a 120 s MANAGEMENT call must not persist an already-expired verdict."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    clock = _AdvancingClock(NOW)
    provider = _AdvancingProvider(
        clock,
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
    )
    service = ManagementService(
        store=container.store,
        clock=clock,
        provider=provider,
        settings=setup["settings"],
        kill_switch=KillSwitch(settings=setup["settings"], store=container.store, clock=clock),
    )

    result = service.manage(position["position_id"], idempotency_key="mgmt-slow-llm")

    assert result.status == "EXECUTED"
    order = json.loads(_orders_by_purpose(container, "PARTIAL_CLOSE")[0].payload_json)
    verdict = container.store.get_risk_verdict(order["risk_verdict_id"])
    assert verdict is not None
    assert verdict.computed_at == "2026-10-08T00:02:00.000Z"
    assert verdict.expires_at > verdict.computed_at


def test_a_full_close_with_an_ignored_invalid_stop_still_executes(tmp_path: Path) -> None:
    """M3: a stopped-through/invalid stop on FULL_CLOSE never rejects the exit."""
    setup = _setup(tmp_path, management_fixture="management_full_close_bad_stop.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    result = setup["service"].manage(position_id, idempotency_key="mgmt-full-bad-stop")

    assert result.status == "EXECUTED"
    assert result.action == "FULL_CLOSE"
    assert result.trade_log is not None
    assert _position_payload(container, position_id)["state"] == "CLOSED"
    assert len(container.store.list_trade_logs(limit=None)) == 1
    assert _stop_update_count(container) == 0
