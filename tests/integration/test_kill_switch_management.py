"""Integration: the kill switch permits risk-reducing management and blocks adds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_management_flow import _orders_by_purpose, _position_payload, _setup

from oft.application.management_service import ManagementRejected


def _classification_events(container) -> list[dict]:  # type: ignore[no-untyped-def]
    events: list[dict] = []
    for event in container.store.list_audit_events_ascending():
        if event.event_type != "MANAGEMENT_CLASSIFIED":
            continue
        body = json.loads(event.payload_json)
        events.append(body.get("payload") or {})
    return events


def test_activating_the_kill_switch_never_auto_closes_a_position(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]

    setup["kill"].activate(actor_id="tester")

    payload = _position_payload(container, position["position_id"])
    assert payload["state"] == "OPEN"
    assert container.store.list_trade_logs(limit=None) == ()
    assert _orders_by_purpose(container, "PARTIAL_CLOSE") == []
    assert _orders_by_purpose(container, "FULL_CLOSE") == []


def test_an_add_is_blocked_with_kill_switch_active_and_the_classification_is_audited(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    setup["kill"].activate(actor_id="tester")
    orders_before = container.store.count_orders()

    with pytest.raises(ManagementRejected) as excinfo:
        setup["service"].manage(position_id, idempotency_key="kill-add")

    assert excinfo.value.code == "KILL_SWITCH_ACTIVE"
    assert container.store.count_orders() == orders_before
    assert _position_payload(container, position_id)["size"] == position["size"]
    classifications = _classification_events(container)
    assert classifications
    assert classifications[-1]["action"] == "ADD"
    assert classifications[-1]["risk_increasing"] is True


def test_a_partial_close_remains_allowed_and_audited_while_kill_switch_is_active(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    position = setup["position"]
    setup["kill"].activate(actor_id="tester")

    result = setup["service"].manage(position["position_id"], idempotency_key="kill-partial")

    assert result.status == "EXECUTED"
    assert len(_orders_by_purpose(container, "PARTIAL_CLOSE")) == 1
    classifications = _classification_events(container)
    assert classifications[-1]["action"] == "PARTIAL_CLOSE"
    assert classifications[-1]["risk_increasing"] is False


def test_a_full_close_remains_allowed_while_kill_switch_is_active(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_full_close.json")
    container = setup["container"]
    position = setup["position"]

    setup["kill"].activate(actor_id="tester")
    result = setup["service"].manage(position["position_id"], idempotency_key="kill-full")

    assert result.status == "EXECUTED"
    assert result.trade_log is not None
    assert len(container.store.list_trade_logs(limit=None)) == 1


def test_a_tightened_stop_is_allowed_and_a_loosened_stop_is_blocked(
    tmp_path: Path,
) -> None:
    tighten = _setup(tmp_path / "tighten", management_fixture="management_tighten_stop.json")
    tighten["kill"].activate(actor_id="tester")

    allowed = tighten["service"].manage(
        tighten["position"]["position_id"], idempotency_key="kill-tighten"
    )

    assert allowed.status == "EXECUTED"
    assert (
        _position_payload(tighten["container"], tighten["position"]["position_id"])["stop_price"]
        == "69500.00"
    )

    loosen = _setup(tmp_path / "loosen", management_fixture="management_loosen_stop.json")
    loosen["kill"].activate(actor_id="tester")
    orders_before = loosen["container"].store.count_orders()

    with pytest.raises(ManagementRejected) as excinfo:
        loosen["service"].manage(loosen["position"]["position_id"], idempotency_key="kill-loosen")

    assert excinfo.value.code == "KILL_SWITCH_ACTIVE"
    classifications = _classification_events(loosen["container"])
    assert classifications[-1]["risk_increasing"] is True
    assert classifications[-1]["stop_change"] == "LOOSEN"
    assert loosen["container"].store.count_orders() == orders_before
    assert (
        _position_payload(loosen["container"], loosen["position"]["position_id"])["stop_price"]
        == loosen["position"]["stop_price"]
    )
