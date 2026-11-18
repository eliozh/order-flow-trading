"""Write-only Telegram token lifecycle through the slice-53 surfaces (F10f)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.telegram import MemorySecretBackend
from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
FIRST = "123456789:AAFirstSentinelToken_abcdefghijklmnopq"
ROTATED = "987654321:BBRotatedSentinelToken_abcdefghijklmnop"
TOKEN_PATH = "/api/v1/settings/telegram/token"
SETTINGS_PATH = "/api/v1/settings/telegram"


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


def _headers(app: Any, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}", "Idempotency-Key": key}


def _set(client: TestClient, app: Any, value: str, key: str) -> Any:
    return client.put(
        TOKEN_PATH,
        json={"bot_token": value},
        headers=_headers(app, key),
    )


def test_set_rotate_and_delete_never_return_the_value(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        first = _set(client, app, FIRST, "token-1")
        assert first.status_code == 200, first.text
        assert first.json()["data"]["masked"] == "****nopq"
        assert FIRST not in first.text
        assert FIRST[:12] not in first.text

        aggregate = client.get(SETTINGS_PATH, headers=_headers(app, "read"))
        assert aggregate.status_code == 200
        assert aggregate.json()["data"]["token"]["masked"] == "****nopq"
        assert FIRST not in aggregate.text

        rotated = _set(client, app, ROTATED, "token-2")
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["data"]["masked"] == "****mnop"
        assert ROTATED not in rotated.text
        assert backend.values["telegram.bot_token"] == ROTATED

        deleted = client.delete(TOKEN_PATH, headers=_headers(app, "token-delete"))
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] == {
            "configured": False,
            "masked": None,
            "last4": None,
            "source": "unset",
            "updated_at": None,
        }
        assert "telegram.bot_token" not in backend.values

        # After deletion the aggregate reports an unconfigured token and the
        # status surface is honest: pushes cannot be delivered.
        after = client.get(SETTINGS_PATH, headers=_headers(app, "read-2")).json()["data"]
        assert after["token"]["configured"] is False
        assert after["token"]["masked"] is None
        status = client.get(
            "/api/v1/telegram/status", headers={"Authorization": f"Bearer {app.state.api_token}"}
        )
        assert status.json()["data"]["state"] == "NOT_CONFIGURED"


def test_delete_leaves_the_allowlist_intact_but_pushes_disabled(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _set(client, app, FIRST, "token-1")
        saved = client.put(
            SETTINGS_PATH,
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers=_headers(app, "settings-1"),
        )
        assert saved.status_code == 200, saved.text
        client.delete(TOKEN_PATH, headers=_headers(app, "token-delete"))

        data = client.get(SETTINGS_PATH, headers=_headers(app, "read")).json()["data"]

    assert data["chat_ids"] == [1001]
    assert data["allowed_user_ids"] == [2002]
    assert data["token"]["configured"] is False
    # The test control cannot succeed without a token.
    assert data["toggles"]["scan_report"] is True


def test_token_validation_refuses_bad_shapes_without_echo(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = _set(client, app, "not-a-telegram-token", "bad-token")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "not-a-telegram-token" not in response.text
    assert backend.values == {}


def test_no_response_log_or_audit_row_contains_the_token(
    tmp_path: Path, backend: MemorySecretBackend, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app(tmp_path)
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        with TestClient(app) as client:
            _set(client, app, FIRST, "token-1")
            bodies = [
                client.get(SETTINGS_PATH, headers=_headers(app, "read")),
                client.get(
                    "/api/v1/telegram/status",
                    headers={"Authorization": f"Bearer {app.state.api_token}"},
                ),
                client.get(
                    "/api/v1/settings",
                    headers={"Authorization": f"Bearer {app.state.api_token}"},
                ),
            ]

        # ``create_app`` calls ``configure_logging(force=True)``, which removes
        # pytest's caplog handler. Re-attach it after the redacting root
        # handlers so this assertion covers a real, scrubbed record instead of
        # an always-empty capture.
        root.addHandler(caplog.handler)
        caplog.set_level(logging.INFO)
        logging.getLogger("oft.test").info("telegram token %s", FIRST)
        assert caplog.records, "no log record was captured; the assertion would be vacuous"
        assert "telegram token" in caplog.text
        assert FIRST not in caplog.text

        for response in bodies:
            assert FIRST not in response.text
            assert FIRST[:10] not in response.text
        audits = app.state.container.store.list_audit_events()
        assert FIRST not in json.dumps([json.loads(event.payload_json) for event in audits])
    finally:
        root.handlers[:] = original_handlers
