"""Integration: a manage request is idempotent and a changed payload conflicts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_management_flow import (
    FIXTURES,
    NOW,
    _orders_by_purpose,
    _position_payload,
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
