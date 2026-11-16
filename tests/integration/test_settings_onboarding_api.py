"""``/api/v1/settings/onboarding`` progress integration tests (slice 44)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.api.auth import CSRF_COOKIE, allowed_origins
from oft.application.settings_service import ONBOARDING_PROGRESS_KEY
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
ONBOARDING_KEY = ONBOARDING_PROGRESS_KEY
SECRET_MARKERS = (
    "api_key",
    "api_secret",
    "token",
    "password",
    "secret",
    "openai_api_key",
    "anthropic_api_key",
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put(client: TestClient, auth: dict[str, str], step: Any, *, key: str = "onboarding-key-1"):
    return client.put(
        "/api/v1/settings/onboarding",
        json={"step": step},
        headers={**auth, "Idempotency-Key": key},
    )


def _seed_corrupt(app: Any, value_json: str = "not-json") -> None:
    app.state.container.store.set_config(
        ConfigRecord(
            key=ONBOARDING_KEY,
            value_json=value_json,
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )


def test_fresh_install_reports_step_1_paper_and_requires_no_config(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/onboarding", headers=_auth(app))

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["environment"] == "paper"
    assert data["completed_steps"] == []
    assert data["resumed_step"] == 1
    assert data["all_complete"] is False
    assert data["updated_at"] is None
    assert data["updated_by"] is None
    assert len(data["steps"]) == 5
    assert data["steps"][0] == {
        "step": 1,
        "id": "environment",
        "title": "环境与本地绑定",
        "deferred": False,
        "deferred_milestone": None,
        "completed": False,
    }
    assert data["steps"][2]["id"] == "binance"
    assert data["steps"][2]["deferred"] is True
    assert data["steps"][2]["deferred_milestone"] == 5
    assert data["steps"][3]["id"] == "telegram"
    assert data["steps"][3]["deferred_milestone"] == 6
    assert envelope["meta"]["provenance"] == "CALC"
    assert envelope["meta"]["data_quality"] == "COMPLETE"
    assert app.state.container.store.list_config() == ()
    assert not any(marker in json.dumps(data) for marker in SECRET_MARKERS)


def test_put_step_1_persists_one_row_one_audit_and_resumes_2(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put(client, auth, 1)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["completed_steps"] == [1]
        assert data["resumed_step"] == 2
        assert data["all_complete"] is False
        assert data["updated_at"] == "2026-11-06T10:00:00.000Z"
        assert data["updated_by"] == "web"
        assert data["steps"][0]["completed"] is True

        rows = app.state.container.store.list_config()
        assert len(rows) == 1
        assert rows[0].key == ONBOARDING_KEY
        assert json.loads(rows[0].value_json) == {"completed_steps": [1]}

        audits = app.state.container.store.list_audit_events()
        assert len(audits) == 1
        assert audits[0].event_type == "CONFIG_UPDATED"
        body = json.loads(audits[0].payload_json)
        assert body["subject"] == {"kind": "CONFIG", "id": ONBOARDING_KEY}
        assert body["actor"] == {"kind": "USER", "id": "web"}
        assert body["payload"]["before"] == []
        assert body["payload"]["after"] == [1]
        assert body["payload"]["source_before"] == "default"
        assert body["payload"]["source_after"] == "user"

        fetched = client.get("/api/v1/settings/onboarding", headers=auth).json()["data"]
        assert fetched["completed_steps"] == [1]
        assert fetched["resumed_step"] == 2


def test_put_step_2_after_step_1_resumes_5_and_skips_deferred_steps(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put(client, auth, 1, key="k1").status_code == 200
        response = _put(client, auth, 2, key="k2")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

    assert data["completed_steps"] == [1, 2]
    assert data["resumed_step"] == 5
    steps = {step["step"]: step for step in data["steps"]}
    assert steps[3]["deferred"] is True
    assert steps[3]["deferred_milestone"] == 5
    assert steps[3]["completed"] is False
    assert steps[4]["deferred"] is True
    assert steps[4]["deferred_milestone"] == 6
    assert steps[5]["deferred"] is False
    assert steps[5]["completed"] is False


def test_all_active_steps_complete_keeps_resumed_step_5(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        for index, step in enumerate((1, 2, 5)):
            assert _put(client, auth, step, key=f"k{index}").status_code == 200
        response = client.get("/api/v1/settings/onboarding", headers=auth)

    data = response.json()["data"]
    assert data["completed_steps"] == [1, 2, 5]
    assert data["resumed_step"] == 5
    assert data["all_complete"] is True


def test_idempotent_replay_returns_stored_envelope_and_writes_nothing(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, 1, key="stable-key")
        second = _put(client, auth, 1, key="stable-key")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["meta"]["idempotent_replay"] is True
    assert second.json()["data"] == first.json()["data"]
    assert len(app.state.container.store.list_config()) == 1
    assert len(app.state.container.store.list_audit_events()) == 1


def test_completing_the_same_step_with_a_new_key_is_a_noop(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put(client, auth, 1, key="first").status_code == 200
        before_audits = app.state.container.store.list_audit_events()
        response = _put(client, auth, 1, key="second")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["completed_steps"] == [1]
    assert response.json()["meta"]["idempotent_replay"] is False
    assert len(app.state.container.store.list_config()) == 1
    assert app.state.container.store.list_audit_events() == before_audits


@pytest.mark.parametrize("step", [3, 4])
def test_deferred_steps_are_rejected_and_write_nothing(tmp_path: Path, step: int) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), step)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "step"
    assert error["details"][0]["code"] == "step_deferred"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


@pytest.mark.parametrize("step", [0, 6, "1", None, 1.5, True])
def test_out_of_range_and_non_integer_steps_are_validation_failures(
    tmp_path: Path, step: Any
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), step)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "step"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_corrupt_persisted_progress_is_config_invalid_on_read_and_write(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)
    _seed_corrupt(app)

    with TestClient(app) as client:
        read = client.get("/api/v1/settings/onboarding", headers=auth)
        assert read.status_code == 500, read.text
        assert read.json()["error"]["code"] == "CONFIG_INVALID"

        write = _put(client, auth, 2, key="corrupt-write")
        assert write.status_code == 500, write.text
        assert write.json()["error"]["code"] == "CONFIG_INVALID"

    row = app.state.container.store.get_config(ONBOARDING_KEY)
    assert row is not None
    assert row.value_json == "not-json"
    assert app.state.container.store.list_audit_events() == ()


def test_structured_corrupt_progress_is_config_invalid(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    _seed_corrupt(app, json.dumps({"completed_steps": "1"}))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/onboarding", headers=_auth(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_responses_never_contain_or_echo_secret_shaped_values(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, 1)
        second = _put(client, auth, 2, key="k2")
        fetched = client.get("/api/v1/settings/onboarding", headers=auth)

    for response in (first, second, fetched):
        body = json.dumps(response.json())
        assert not any(marker in body for marker in SECRET_MARKERS)
        # The request carries only an integer step; nothing is echoed back as a
        # free-form string.
        assert "step" in body


def test_missing_idempotency_key_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/onboarding",
            json={"step": 1},
            headers=_auth(app),
        )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_extra_field_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/onboarding",
            json={"step": 1, "extra": True},
            headers={**_auth(app), "Idempotency-Key": "extra-field"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_unauthenticated_read_and_write_are_rejected(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        read = client.get("/api/v1/settings/onboarding")
        write = client.put(
            "/api/v1/settings/onboarding",
            json={"step": 1},
            headers={"Idempotency-Key": "no-auth"},
        )

    for response in (read, write):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert app.state.container.store.list_config() == ()


def test_browser_put_without_csrf_token_is_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        minted = client.get("/", params={"bootstrap": secret}, follow_redirects=False)
        assert minted.status_code == 302, minted.text
        assert client.get("/health").status_code == 200
        assert client.cookies.get(CSRF_COOKIE)
        origin = sorted(allowed_origins(settings))[0]
        response = client.put(
            "/api/v1/settings/onboarding",
            json={"step": 1},
            headers={"Origin": origin, "Idempotency-Key": "csrf-missing"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "CSRF_INVALID"
    assert app.state.container.store.list_config() == ()


@pytest.mark.parametrize("raw_steps", ["[true]", "[1.5]", "[9]", '["1"]'])
def test_decoder_rejects_bool_float_and_out_of_range_persisted_values(
    tmp_path: Path, raw_steps: str
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    _seed_corrupt(app, f'{{"completed_steps": {raw_steps}}}')

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/onboarding", headers=_auth(app))

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    # The corrupt row is never overwritten by a failed read.
    row = app.state.container.store.get_config(ONBOARDING_KEY)
    assert row is not None
    assert json.loads(row.value_json) == {"completed_steps": json.loads(raw_steps)}
