"""Integration: a manage request is idempotent and a changed payload conflicts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_management_flow import (
    FIXTURES,
    NOW,
    _orders_by_purpose,
    _position_payload,
    _rewrite_position_payload,
    _setup,
)

from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.application.management_service import ManagementRejected
from oft.clock import FixedClock
from oft.ports.llm import LLMRole
from oft.ports.store import DecisionRecord


def _auth(app) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_a_duplicate_service_call_returns_the_original_and_creates_no_rows(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    first = setup["service"].manage(position_id, idempotency_key="dup-manage")
    orders_after_first = container.store.count_orders()
    fills_after_first = container.store.count_fills()
    size_after_first = _position_payload(container, position_id)["size"]

    second = setup["service"].manage(position_id, idempotency_key="dup-manage")

    assert second.idempotent_replay is True
    assert second.order is not None and first.order is not None
    assert second.order["order_id"] == first.order["order_id"]
    assert second.fill is not None and first.fill is not None
    assert second.fill["fill_id"] == first.fill["fill_id"]
    assert second.trade_log is None
    assert container.store.count_orders() == orders_after_first
    assert container.store.count_fills() == fills_after_first
    assert _position_payload(container, position_id)["size"] == size_after_first
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1


def test_the_same_key_with_a_different_provider_is_a_service_conflict(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    position = setup["position"]

    setup["service"].manage(position["position_id"], idempotency_key="dup-provider")

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(
            position["position_id"],
            idempotency_key="dup-provider",
            provider_name="openai",
        )
    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"


def test_the_api_replays_a_duplicate_manage_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    position = setup["position"]
    monkeypatch.setattr(
        "oft.bootstrap.build_llm_adapter",
        lambda *_: LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_partial_close.json"},
        ),
    )
    app = create_app(setup["settings"], clock=FixedClock(NOW))
    headers = _auth(app)

    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/positions/{position['position_id']}/manage",
            json={"provider": "stub"},
            headers={**headers, "Idempotency-Key": "api-manage-1"},
        )
        assert first.status_code == 200, first.text
        first_data = first.json()["data"]
        assert first.json()["meta"]["idempotent_replay"] is False
        order_id = first_data["order"]["order_id"]

        second = client.post(
            f"/api/v1/positions/{position['position_id']}/manage",
            json={"provider": "stub"},
            headers={**headers, "Idempotency-Key": "api-manage-1"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["meta"]["idempotent_replay"] is True
        assert second.json()["data"]["order"]["order_id"] == order_id

        conflict = client.post(
            f"/api/v1/positions/{position['position_id']}/manage",
            json={"provider": "openai"},
            headers={**headers, "Idempotency-Key": "api-manage-1"},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_a_manage_request_without_an_idempotency_key_is_refused(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    position = setup["position"]
    app = create_app(setup["settings"], clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/positions/{position['position_id']}/manage",
            json={"provider": "stub"},
            headers=_auth(app),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_duplicate_hold_does_not_append_a_second_history_entry(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    setup["service"].manage(position_id, idempotency_key="dup-hold")
    setup["service"].manage(position_id, idempotency_key="dup-hold")

    history = [
        entry
        for entry in _position_payload(container, position_id)["management_history"]
        if entry["action"] == "HOLD"
    ]
    assert len(history) == 1


def test_a_replay_for_a_vanished_position_is_a_typed_not_found(tmp_path: Path) -> None:
    """M5: a service POSITION_NOT_FOUND maps to a typed 404, never a 500."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    position_id = "POS-GHOST"
    key = "ghost-key"
    digest = hashlib.sha256(f"{position_id}:{key}".encode()).hexdigest()[:16]
    decision_id = f"MGD-{digest.upper()}"
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="MANAGEMENT",
            decision_type="POSITION_MANAGEMENT",
            validation_status="VALID",
            raw_output_hash="staged",
            persisted_at=NOW.isoformat(),
            payload_json=json.dumps(
                {
                    "validation_status": "VALID",
                    "position_management": {"action": "HOLD"},
                },
                sort_keys=True,
            ),
        )
    )
    app = create_app(setup["settings"], clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/positions/{position_id}/manage",
            json={"provider": "stub"},
            headers={**_auth(app), "Idempotency-Key": key},
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def _execute(setup, sql: str, parameters: tuple[object, ...]) -> None:
    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(sql, parameters)
        connection.commit()


def _stage_decision(setup, *, position_id: str, key: str, payload_json: str) -> str:
    digest = hashlib.sha256(f"{position_id}:{key}".encode()).hexdigest()[:16]
    decision_id = f"MGD-{digest.upper()}"
    setup["container"].store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="MANAGEMENT",
            decision_type="POSITION_MANAGEMENT",
            validation_status="VALID",
            raw_output_hash="staged",
            persisted_at=NOW.isoformat(),
            payload_json=payload_json,
        )
    )
    return decision_id


def test_a_transient_missing_contract_re_attempts_after_it_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: a refused (not executed) valid decision re-attempts on replay."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    store = container.store
    real_get_symbol = store.get_symbol
    monkeypatch.setattr(store, "get_symbol", lambda symbol: None)

    with pytest.raises(ManagementRejected) as first:
        setup["service"].manage(position_id, idempotency_key="retry-contract")

    assert first.value.code == "DATA_UNAVAILABLE"
    assert _orders_by_purpose(container, "PARTIAL_CLOSE") == []
    monkeypatch.setattr(store, "get_symbol", real_get_symbol)

    replay = setup["service"].manage(position_id, idempotency_key="retry-contract")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert replay.executed is True
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1


def test_a_kill_switch_refusal_re_attempts_after_deactivation(tmp_path: Path) -> None:
    """H3: a refused ADD re-attempts once the kill switch is clear."""
    from oft.application.kill_switch import RESUME_TOKEN

    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    setup["kill"].activate(actor_id="tester")
    orders_before = container.store.count_orders()

    with pytest.raises(ManagementRejected) as first:
        setup["service"].manage(position_id, idempotency_key="retry-kill")

    assert first.value.code == "KILL_SWITCH_ACTIVE"
    assert container.store.count_orders() == orders_before
    assert _orders_by_purpose(container, "ADD") == []

    setup["kill"].deactivate(token=RESUME_TOKEN, actor_id="tester")
    replay = setup["service"].manage(position_id, idempotency_key="retry-kill")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert len(_orders_by_purpose(container, "ADD")) == 1


def test_a_blocked_add_replays_as_blocked_with_its_risk_evidence(tmp_path: Path) -> None:
    """H3: a deterministic risk-gate block is terminal, never re-attempted."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    container.store.set_system_state(
        "risk_caps",
        json.dumps({"max_concurrent_positions": 1}),
        updated_at=NOW.isoformat(),
    )
    orders_before = container.store.count_orders()

    first = setup["service"].manage(position_id, idempotency_key="replay-blocked")
    replay = setup["service"].manage(position_id, idempotency_key="replay-blocked")

    assert first.status == "BLOCKED"
    assert replay.status == "BLOCKED"
    assert replay.executed is False
    assert replay.idempotent_replay is True
    assert replay.risk is not None
    assert replay.risk["binding_cap"] == "MAX_POSITIONS"
    assert container.store.count_orders() == orders_before


def test_a_rejected_management_decision_replays_as_the_same_typed_rejection(
    tmp_path: Path,
) -> None:
    """H3: a REJECTED decision is terminal and never re-executed."""
    setup = _setup(tmp_path, management_fixture="management_invalid_stop.json")
    container = setup["container"]
    position = setup["position"]
    orders_before = container.store.count_orders()

    with pytest.raises(ManagementRejected) as first:
        setup["service"].manage(position["position_id"], idempotency_key="replay-rejected")
    with pytest.raises(ManagementRejected) as second:
        setup["service"].manage(position["position_id"], idempotency_key="replay-rejected")

    assert first.value.code == "LLM_SCHEMA_INVALID"
    assert second.value.code == "LLM_SCHEMA_INVALID"
    assert container.store.count_orders() == orders_before


def test_a_full_close_replay_attaches_the_matching_trade_log(tmp_path: Path) -> None:
    """M1/H2: only a FULL_CLOSE replay attaches the decision's trade log."""
    setup = _setup(tmp_path, management_fixture="management_full_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]

    first = setup["service"].manage(position_id, idempotency_key="replay-full")
    orders = container.store.count_orders()
    trades = len(container.store.list_trade_logs(limit=None))
    replay = setup["service"].manage(position_id, idempotency_key="replay-full")

    assert first.trade_log is not None
    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert replay.order is not None and replay.fill is not None
    assert replay.trade_log is not None
    assert replay.trade_log["trade_id"] == first.trade_log["trade_id"]
    assert container.store.count_orders() == orders
    assert len(container.store.list_trade_logs(limit=None)) == trades


def test_a_full_close_replay_without_the_trade_log_is_data_unavailable(
    tmp_path: Path,
) -> None:
    """H2: EXECUTED is never reported with missing journal evidence."""
    setup = _setup(tmp_path, management_fixture="management_full_close.json")
    position = setup["position"]
    position_id = position["position_id"]
    setup["service"].manage(position_id, idempotency_key="replay-no-trade")
    _execute(setup, "DELETE FROM trade_logs WHERE position_id = ?", (position_id,))

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="replay-no-trade")

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_an_executed_replay_without_the_fill_is_data_unavailable(tmp_path: Path) -> None:
    """H2: an executed replay requires the order's fill."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    position = setup["position"]
    position_id = position["position_id"]
    first = setup["service"].manage(position_id, idempotency_key="replay-no-fill")
    assert first.order is not None
    _execute(setup, "DELETE FROM fills WHERE order_id = ?", (first.order["order_id"],))

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="replay-no-fill")

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_a_corrupt_stored_envelope_is_data_unavailable(tmp_path: Path) -> None:
    """H3: a corrupt stored decision fails closed, never defaults to HOLD."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    position = setup["position"]

    _stage_decision(
        setup,
        position_id=position["position_id"],
        key="corrupt-envelope",
        payload_json="{not valid json",
    )

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key="corrupt-envelope")

    assert excinfo.value.code == "DATA_UNAVAILABLE"


@pytest.mark.parametrize(
    "payload_json",
    (
        '{"validation_status": "VALID"}',
        '{"position_management": "HOLD"}',
        '{"position_management": {"action": "SCALE_IN"}}',
    ),
)
def test_an_unreadable_stored_management_payload_is_data_unavailable(
    tmp_path: Path, payload_json: str
) -> None:
    """H3: missing/invalid payloads and unknown actions fail closed."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    position = setup["position"]
    key = f"bad-payload-{abs(hash(payload_json))}"

    _stage_decision(
        setup,
        position_id=position["position_id"],
        key=key,
        payload_json=payload_json,
    )

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position["position_id"], idempotency_key=key)

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_a_lost_history_entry_reconstructs_from_the_deterministic_order(
    tmp_path: Path,
) -> None:
    """H3: an order committed but its history write was lost -> reconstruct it."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    first = setup["service"].manage(position_id, idempotency_key="retry-lost-history")
    assert first.order is not None
    orders_after_first = container.store.count_orders()
    staged = _position_payload(container, position_id)
    staged["management_history"] = []
    _rewrite_position_payload(setup["settings"], position_id, staged)

    replay = setup["service"].manage(position_id, idempotency_key="retry-lost-history")

    assert replay.idempotent_replay is True
    assert replay.status == "EXECUTED"
    assert replay.order is not None
    assert replay.order["order_id"] == first.order["order_id"]
    assert replay.fill is not None
    assert container.store.count_orders() == orders_after_first


def test_a_replayed_order_from_a_different_decision_is_data_unavailable(
    tmp_path: Path,
) -> None:
    """M1/H2: an order bound to another decision is never replayed as this one."""
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    position = setup["position"]
    position_id = position["position_id"]
    first = setup["service"].manage(position_id, idempotency_key="mismatched-order")
    assert first.order is not None

    _execute(
        setup,
        "UPDATE orders SET decision_id = ? WHERE order_id = ?",
        ("MGD-OTHER-DECISION", first.order["order_id"]),
    )

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="mismatched-order")

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_a_full_close_trade_log_from_a_different_decision_is_data_unavailable(
    tmp_path: Path,
) -> None:
    """M1/H2: a trade log bound to another decision is never attached."""
    setup = _setup(tmp_path, management_fixture="management_full_close.json")
    position = setup["position"]
    position_id = position["position_id"]
    first = setup["service"].manage(position_id, idempotency_key="mismatched-trade-log")
    assert first.trade_log is not None
    staged_decision = _stage_decision(
        setup,
        position_id=position_id,
        key="other-decision",
        payload_json='{"validation_status": "VALID", "position_management": {"action": "HOLD"}}',
    )
    assert staged_decision != first.decision_id

    _execute(
        setup,
        "UPDATE trade_logs SET decision_id = ? WHERE position_id = ?",
        (staged_decision, position_id),
    )

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="mismatched-trade-log")

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_the_manage_route_returns_the_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM B: the response model carries the typed RISK_BLOCKED refusal."""
    from oft.api.schemas.position import PositionManageData

    setup = _setup(tmp_path, management_fixture="management_loosen_stop.json")
    position = setup["position"]
    monkeypatch.setattr(
        "oft.bootstrap.build_llm_adapter",
        lambda *_: LLMStub(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: "management_loosen_stop.json"},
        ),
    )
    app = create_app(setup["settings"], clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/positions/{position['position_id']}/manage",
            json={"provider": "stub"},
            headers={**_auth(app), "Idempotency-Key": "api-manage-refusal"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "EXECUTED"
    assert data["refusals"] == [
        {
            "code": "RISK_BLOCKED",
            "field": "position_management.new_stop_price",
            "message": "a management action may not loosen or remove the protective stop",
        }
    ]
    # The response model constrains the wire shape (extra keys/fields forbidden).
    assert PositionManageData.model_validate(data).refusals[0].code == "RISK_BLOCKED"
