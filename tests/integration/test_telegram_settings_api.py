"""Aggregate Telegram settings API (slice 53, F10f).

``GET/PUT /api/v1/settings/telegram`` exposes the token mask, allowlists, six
push toggles, provenance, and recent receipts. Every write is idempotent,
CSRF-protected, and audited with exactly one ``TELEGRAM_SETTINGS_UPDATED`` row.
The token value never appears in any response.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.telegram import MemorySecretBackend
from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.application.telegram_events import (
    BUDGET_THRESHOLD_EVENT_TYPE,
    ENTRY_RECHECK_EVENT_TYPE,
    OPPORTUNITY_TRIGGERED_EVENT_TYPE,
    POSITION_MANAGEMENT_EVENT_TYPE,
    RISK_ALERT_EVENT_TYPE,
    SCAN_SUMMARY_EVENT_TYPE,
)
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
PATH = "/api/v1/settings/telegram"


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _app(tmp_path: Path) -> Any:
    return create_app(
        Settings(env="test", port=8765, data_dir=tmp_path / "data"),
        clock=FixedClock(NOW),
    )


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _get(client: TestClient, app: Any) -> Any:
    return client.get(PATH, headers=_auth(app))


def _put(client: TestClient, app: Any, body: dict[str, Any], key: str = "tg-settings-1") -> Any:
    return client.put(PATH, json=body, headers={**(_auth(app)), "Idempotency-Key": key})


def _set_token(client: TestClient, app: Any, value: str = SENTINEL) -> Any:
    return client.put(
        "/api/v1/settings/telegram/token",
        json={"bot_token": value},
        headers={**_auth(app), "Idempotency-Key": "token-set"},
    )


def test_fresh_install_is_default_with_all_toggles_on_and_no_chats(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = _get(client, app)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["token"] == {
        "configured": False,
        "masked": None,
        "last4": None,
        "source": "unset",
        "updated_at": None,
    }
    assert data["chat_ids"] == []
    assert data["allowed_user_ids"] == []
    assert data["toggles"] == {
        "scan_report": True,
        "trigger": True,
        "entry_recheck": True,
        "position": True,
        "risk": True,
        "budget": True,
    }
    assert data["source"] == "default"
    assert data["updated_at"] is None
    assert data["updated_by"] is None
    assert data["receipts"] == []
    assert data["receipt_total"] == 0
    assert data["latest_test_receipt"] is None
    assert app.state.container.store.list_config() == ()


def test_saving_chats_toggles_writes_one_audit_and_returns_the_aggregate(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        assert _set_token(client, app).status_code == 200
        response = _put(
            client,
            app,
            {
                "chat_ids": [1001, -1002],
                "allowed_user_ids": [2002],
                "toggles": {"scan_report": False, "budget": False},
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["chat_ids"] == [-1002, 1001]
    assert data["allowed_user_ids"] == [2002]
    assert data["toggles"]["scan_report"] is False
    assert data["toggles"]["budget"] is False
    assert data["toggles"]["trigger"] is True
    assert data["source"] == "user"
    assert data["updated_at"] == "2026-11-18T09:30:00.000Z"
    assert data["updated_by"] == "web"
    assert SENTINEL not in response.text

    audits = app.state.container.store.list_audit_events()
    assert [event.event_type for event in audits].count("TELEGRAM_SETTINGS_UPDATED") == 1
    audit = next(e for e in audits if e.event_type == "TELEGRAM_SETTINGS_UPDATED")
    payload = json.loads(audit.payload_json)
    assert payload["actor"] == {"kind": "USER", "id": "web"}
    assert payload["subject"] == {"kind": "SYSTEM", "id": "telegram"}
    assert payload["payload"]["after"]["chat_ids"] == [-1002, 1001]
    assert payload["payload"]["after"]["toggles"]["scan_report"] is False
    assert payload["payload"]["before"]["chat_ids"] == []


def test_enabling_a_toggle_without_a_chat_is_rejected_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    before = app.state.container.store.list_config()

    with TestClient(app) as client:
        # All toggles default on and there is no chat: the all-on/empty state is
        # only reachable by reading defaults, never by a save.
        response = _put(client, app, {"toggles": {"risk": True}})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert any(detail["code"] == "chat_required_for_pushes" for detail in error["details"])
    assert app.state.container.store.list_config() == before
    assert not any(
        event.event_type == "TELEGRAM_SETTINGS_UPDATED"
        for event in app.state.container.store.list_audit_events()
    )


def test_all_off_with_no_chats_is_allowed(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = _put(
            client,
            app,
            {
                "chat_ids": [],
                "allowed_user_ids": [],
                "toggles": {
                    "scan_report": False,
                    "trigger": False,
                    "entry_recheck": False,
                    "position": False,
                    "risk": False,
                    "budget": False,
                },
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["chat_ids"] == []
    assert data["toggles"] == {
        "scan_report": False,
        "trigger": False,
        "entry_recheck": False,
        "position": False,
        "risk": False,
        "budget": False,
    }


def test_partial_patch_keeps_the_untouched_allowlist_and_toggles(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]}, key="first")
        response = _put(client, app, {"toggles": {"trigger": False}}, key="second")

    data = response.json()["data"]
    assert data["chat_ids"] == [1001]
    assert data["allowed_user_ids"] == [2002]
    assert data["toggles"]["trigger"] is False
    assert data["toggles"]["scan_report"] is True


def test_an_unchanged_save_is_a_noop(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        first = _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]}, key="a")
        assert first.status_code == 200
        audits_before = app.state.container.store.list_audit_events()
        rows_before = app.state.container.store.list_config()
        second = _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]}, key="b")

    assert second.status_code == 200, second.text
    assert app.state.container.store.list_config() == rows_before
    assert app.state.container.store.list_audit_events() == audits_before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"chat_ids": [0]},
        {"chat_ids": [True]},
        {"chat_ids": ["1001"]},
        {"allowed_user_ids": [-1]},
        {"allowed_user_ids": [1.5]},
        {"toggles": {"unknown": True}},
        {"toggles": {"risk": "yes"}},
    ],
)
def test_malformed_patches_are_rejected_and_write_nothing(
    tmp_path: Path, backend: MemorySecretBackend, body: dict[str, Any]
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = _put(client, app, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_ids_are_bounded(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        too_many_chats = _put(client, app, {"chat_ids": list(range(1, 12))})
        too_many_users = _put(client, app, {"allowed_user_ids": list(range(1, 22))})

    for response in (too_many_chats, too_many_users):
        assert response.status_code == 422, response.text
        assert any(detail["code"] == "too_many" for detail in response.json()["error"]["details"])


def test_identical_replay_returns_the_stored_envelope_and_conflicts_on_a_new_payload(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        first = _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]}, key="stable")
        replay = _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]}, key="stable")
        conflict = _put(client, app, {"chat_ids": [3003]}, key="stable")

    assert first.status_code == replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert replay.json()["data"] == first.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_idempotency_key_is_rejected(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = client.put(PATH, json={"chat_ids": [1001]}, headers=_auth(app))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert app.state.container.store.list_config() == ()


def test_recent_receipts_and_latest_test_are_included(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _set_token(client, app)
        _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]})
        test_response = client.post(
            "/api/v1/telegram/test",
            json={},
            headers={**_auth(app), "Idempotency-Key": "test-1"},
        )
        assert test_response.status_code == 200, test_response.text
        response = _get(client, app)

    data = response.json()["data"]
    assert data["receipt_total"] == 1
    assert len(data["receipts"]) == 1
    assert data["receipts"][0]["kind"] == "test"
    assert data["latest_test_receipt"]["kind"] == "test"
    assert SENTINEL not in response.text


def test_the_settings_aggregate_includes_the_telegram_section(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        fresh = client.get("/api/v1/settings", headers=_auth(app))
        assert fresh.status_code == 200, fresh.text
        fresh_sections = {row["section"]: row for row in fresh.json()["data"]["sections"]}
        assert fresh_sections["telegram"]["source"] == "default"
        assert fresh_sections["telegram"]["last_changed"] is None
        assert "telegram.scan_report" in fresh_sections["telegram"]["keys"]

        _put(client, app, {"chat_ids": [1001], "allowed_user_ids": [2002]})
        saved = client.get("/api/v1/settings", headers=_auth(app))

    sections = {row["section"]: row for row in saved.json()["data"]["sections"]}
    assert sections["telegram"]["source"] == "user"
    assert sections["telegram"]["last_changed"] == "2026-11-18T09:30:00.000Z"
    assert "token" not in json.dumps(sections).lower()


def test_browser_put_without_csrf_is_rejected(tmp_path: Path, backend: MemorySecretBackend) -> None:
    from oft.api.auth import CSRF_COOKIE, allowed_origins

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        minted = client.get("/", params={"bootstrap": secret}, follow_redirects=False)
        assert minted.status_code == 302
        assert client.get("/health").status_code == 200
        assert client.cookies.get(CSRF_COOKIE)
        origin = sorted(allowed_origins(settings))[0]
        response = client.put(
            PATH,
            json={"chat_ids": [1001]},
            headers={"Origin": origin, "Idempotency-Key": "csrf-missing"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
    assert app.state.container.store.list_config() == ()


def test_a_saved_toggle_suppresses_the_event_at_enqueue_time(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """A toggle written through the aggregate is read by the one push path."""
    from test_scan_telegram_push import SCAN_PAYLOAD, _harness

    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.client.put(
            PATH,
            json={"toggles": {"scan_report": False}},
            headers={**harness.auth, "Idempotency-Key": "toggle-off"},
        )
        assert response.status_code == 200, response.text
        scan = harness.post_scan(SCAN_PAYLOAD)
    finally:
        harness.close()

    assert scan.status_code == 201, scan.text
    # The scan still completed; only its Telegram push was suppressed.
    assert scan.json()["data"]["scan"]["status"] == "DONE"
    assert harness.container.store.list_telegram_receipts() == ()


# One representative event per operator-facing toggle; each gate must read the
# toggle with the same name, so a name/key/event swap fails this parametrization.
_TOGGLE_EVENT_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "scan_report": (SCAN_SUMMARY_EVENT_TYPE, {"report_ids": ["RPT-1"], "status": "DONE"}),
    "trigger": (OPPORTUNITY_TRIGGERED_EVENT_TYPE, {}),
    "entry_recheck": (ENTRY_RECHECK_EVENT_TYPE, {}),
    "position": (POSITION_MANAGEMENT_EVENT_TYPE, {}),
    "risk": (RISK_ALERT_EVENT_TYPE, {}),
    "budget": (BUDGET_THRESHOLD_EVENT_TYPE, {}),
}


@pytest.mark.parametrize("name", list(_TOGGLE_EVENT_CASES))
def test_each_saved_toggle_suppresses_only_its_own_event(
    tmp_path: Path, backend: MemorySecretBackend, name: str
) -> None:
    """A name↔key↔event swap must fail: every other gate stays enabled."""
    from test_scan_telegram_push import _harness

    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.client.put(
            PATH,
            json={"toggles": {name: False}},
            headers={**harness.auth, "Idempotency-Key": f"toggle-{name}"},
        )
        assert response.status_code == 200, response.text

        state = harness.container.telegram_service.settings_state()
        assert state.toggles[name] is False
        for other_name in _TOGGLE_EVENT_CASES:
            if other_name != name:
                assert state.toggles[other_name] is True

        notifications = harness.container.notifications
        for event_name, (event_type, payload) in _TOGGLE_EVENT_CASES.items():
            assert notifications._event_enabled(event_type, payload) is (event_name != name)
    finally:
        harness.close()
