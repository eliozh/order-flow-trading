"""Telegram status, test send, receipts, and settings API surfaces."""

from __future__ import annotations

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

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _app(tmp_path: Path) -> Any:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    return create_app(settings, clock=FixedClock(NOW))


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _configure(client: TestClient, auth: dict[str, str]) -> None:
    token = client.put(
        "/api/v1/settings/telegram/token",
        json={"bot_token": SENTINEL},
        headers={**auth, "Idempotency-Key": "token-1"},
    )
    allowlist = client.put(
        "/api/v1/settings/telegram/allowlist",
        json={"chat_ids": [1001], "allowed_user_ids": [2002]},
        headers={**auth, "Idempotency-Key": "allow-1"},
    )
    assert token.status_code == 200, token.text
    assert allowlist.status_code == 200, allowlist.text


def _test_send(client: TestClient, auth: dict[str, str], key: str) -> Any:
    return client.post(
        "/api/v1/telegram/test",
        json={},
        headers={**auth, "Idempotency-Key": key},
    )


def test_test_send_requires_a_token(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = _test_send(client, _auth(app), "test-1")

    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "TELEGRAM_NOT_CONFIGURED"


def test_test_send_requires_an_allowlist(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers={**auth, "Idempotency-Key": "token-1"},
        )
        response = _test_send(client, auth, "test-1")

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "TELEGRAM_NOT_CONFIGURED"


def test_test_send_enqueues_a_receipt_without_leaking_the_token(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        _configure(client, auth)
        response = _test_send(client, auth, "test-1")

    assert response.status_code == 200, response.text
    receipt = response.json()["data"]
    assert receipt["kind"] == "test"
    assert receipt["chat_id"] == 1001
    assert receipt["state"] == "queued"
    assert SENTINEL not in response.text


def test_an_identical_test_send_replay_is_idempotent(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        _configure(client, auth)
        first = _test_send(client, auth, "test-1")
        second = _test_send(client, auth, "test-1")

    assert first.status_code == second.status_code == 200
    assert second.json()["meta"]["idempotent_replay"] is True
    assert len(app.state.container.store.list_telegram_receipts()) == 1


def test_receipts_are_bounded_and_newest_first(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        _configure(client, auth)
        for index in range(4):
            assert _test_send(client, auth, f"test-{index}").status_code == 200
        response = client.get("/api/v1/telegram/receipts?limit=2", headers=auth)
        full = client.get("/api/v1/telegram/receipts", headers=auth)

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["items"]) == 2
    assert full.json()["data"]["total"] == 4
    assert data["items"][0]["event_key"] != data["items"][1]["event_key"]
    assert response.json()["data"]["items"][0]["state"] == "queued"


def test_receipts_reject_an_out_of_range_limit(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/v1/telegram/receipts?limit=0", headers=_auth(app))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_the_allowlist_mutation_is_idempotent_and_validated(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        first = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**auth, "Idempotency-Key": "allow-1"},
        )
        replay = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**auth, "Idempotency-Key": "allow-1"},
        )
        invalid = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [0], "allowed_user_ids": []},
            headers={**auth, "Idempotency-Key": "allow-2"},
        )
        read = client.get("/api/v1/settings/telegram/allowlist", headers=auth)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert invalid.status_code == 422
    assert invalid.json()["error"]["details"][0]["field"] == "chat_ids"
    assert read.json()["data"]["chat_ids"] == [1001]
    assert read.json()["data"]["allowed_user_ids"] == [2002]


def test_mutations_require_an_idempotency_key(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers=_auth(app),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_an_invalid_token_surfaces_a_stable_code_and_no_body(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    from oft.ports.telegram import (
        TELEGRAM_TOKEN_INVALID,
        TelegramError,
        TelegramIdentity,
        TelegramUpdatePage,
    )

    class _UnauthorizedPort:
        def get_me(self) -> TelegramIdentity:
            raise TelegramError(TELEGRAM_TOKEN_INVALID, "scripted 401")

        def get_updates(self, *, offset=None, timeout_seconds=0):  # type: ignore[no-untyped-def]
            return TelegramUpdatePage(())

        def send_message(self, *, chat_id, text, reply_to_message_id=None):  # type: ignore[no-untyped-def]
            raise TelegramError(TELEGRAM_TOKEN_INVALID, "scripted 401")

    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        _configure(client, auth)
        app.state.container.telegram_service._port = _UnauthorizedPort()  # noqa: SLF001
        response = _test_send(client, auth, "test-invalid")

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "TELEGRAM_TOKEN_INVALID"
    assert "scripted 401" not in response.text


def test_a_missing_secure_store_makes_a_token_write_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: None)
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers={**_auth(app), "Idempotency-Key": "no-store"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert SENTINEL not in response.text


def test_test_send_to_a_non_allowlisted_chat_is_refused(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)
    with TestClient(app) as client:
        _configure(client, auth)
        response = client.post(
            "/api/v1/telegram/test",
            json={"chat_id": 9999},
            headers={**auth, "Idempotency-Key": "test-wrong-chat"},
        )

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "TELEGRAM_NOT_CONFIGURED"
    assert SENTINEL not in response.text
