"""Write-only Telegram token API: storage, masking, deletion, and no leakage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.telegram import MemorySecretBackend
from oft.adapters.sqlite_schema import database_path
from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
OTHER = "987654321:BBOtherTelegramToken_abcdefghijklmnopqrstuv"


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _app(tmp_path: Path, **overrides: Any) -> Any:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data", **overrides)
    return create_app(settings, clock=FixedClock(NOW))


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put_token(client: TestClient, auth: dict[str, str], value: str, key: str) -> Any:
    return client.put(
        "/api/v1/settings/telegram/token",
        json={"bot_token": value},
        headers={**auth, "Idempotency-Key": key},
    )


def _put_allowlist(client: TestClient, auth: dict[str, str], key: str = "allow-1") -> Any:
    return client.put(
        "/api/v1/settings/telegram/allowlist",
        json={"chat_ids": [1001], "allowed_user_ids": [2002], "reason": "setup"},
        headers={**auth, "Idempotency-Key": key},
    )


def test_the_stored_token_row_carries_only_non_secret_metadata(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put_token(client, auth, SENTINEL, "token-1")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {"configured", "masked", "last4", "source", "updated_at"}
    assert data == {
        "configured": True,
        "masked": "****stuv",
        "last4": "stuv",
        "source": "encrypted_file",
        "updated_at": "2026-11-17T09:30:00.000Z",
    }
    assert SENTINEL not in response.text
    meta = app.state.container.store.get_secret_meta("telegram", "bot_token")
    assert meta is not None
    assert (meta.provider, meta.name, meta.last4, meta.backend) == (
        "telegram",
        "bot_token",
        "stuv",
        "encrypted_file",
    )
    assert SENTINEL not in json.dumps(meta.__dict__)


def test_the_token_never_appears_in_the_sqlite_files(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert _put_token(client, _auth(app), SENTINEL, "token-1").status_code == 200

    db_path = database_path(app.state.settings)
    candidates = [db_path, db_path.with_name(db_path.name + "-wal")]
    for path in candidates:
        if path.exists():
            assert SENTINEL.encode("utf-8") not in path.read_bytes(), path


def test_an_invalid_token_is_rejected_and_nothing_is_written(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = _put_token(client, _auth(app), "not-a-token", "token-1")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "bot_token"
    assert "not-a-token" not in response.text
    assert app.state.container.store.get_secret_meta("telegram", "bot_token") is None


def test_delete_removes_the_stored_token_and_falls_back_to_the_environment(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path, telegram_bot_token=OTHER)
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_token(client, auth, SENTINEL, "token-1").status_code == 200
        deleted = client.delete(
            "/api/v1/settings/telegram/token",
            headers={**auth, "Idempotency-Key": "token-delete"},
        )

    assert deleted.status_code == 200, deleted.text
    data = deleted.json()["data"]
    assert data["configured"] is True
    assert data["source"] == "env"
    assert data["masked"] is None
    assert data["last4"] is None
    assert SENTINEL not in deleted.text and OTHER not in deleted.text


def test_an_identical_replay_is_reported_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put_token(client, auth, SENTINEL, "token-1")
        second = _put_token(client, auth, SENTINEL, "token-1")

    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["meta"]["idempotent_replay"] is True
    audits = [event.event_type for event in app.state.container.store.list_audit_events_ascending()]
    assert audits.count("SECRET_ADDED") == 1


def test_there_is_no_token_reveal_endpoint(tmp_path: Path, backend: MemorySecretBackend) -> None:
    token_path = "/api/v1/settings/telegram/token"
    app = _app(tmp_path)
    auth = _auth(app)

    # No documented operation on the token path may answer GET. The optional SPA
    # catch-all is a GET on "/{full_path:path}", not on the token path itself.
    operations = set(app.openapi()["paths"].get(token_path, {}))
    assert {"put", "delete"} <= operations, operations
    assert "get" not in operations

    with TestClient(app) as client:
        assert _put_token(client, auth, SENTINEL, "token-1").status_code == 200
        response = client.get(token_path, headers=auth)

    # 404 (SPA catch-all or route miss) and 405 (method mismatch, no built SPA)
    # both prove the read route is absent; the test must not depend on web/dist.
    assert response.status_code in {404, 405}, response.text
    assert SENTINEL not in response.text
    expected_code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}[response.status_code]
    assert response.json()["error"]["code"] == expected_code


def test_the_status_transitions_are_deterministic(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    auth = _auth(app)

    with TestClient(app) as client:
        not_configured = client.get("/api/v1/telegram/status", headers=auth)
        assert _put_token(client, auth, SENTINEL, "token-1").status_code == 200
        no_allowlist = client.get("/api/v1/telegram/status", headers=auth)
        chat_only = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [], "reason": "chat only"},
            headers={**auth, "Idempotency-Key": "allow-chat-only"},
        )
        assert chat_only.status_code == 200, chat_only.text
        chat_only_status = client.get("/api/v1/telegram/status", headers=auth)
        assert _put_allowlist(client, auth).status_code == 200
        ready = client.get("/api/v1/telegram/status", headers=auth)
        # The metadata row survives but the backend value disappears: the
        # runtime now refuses rather than silently falling back.
        backend.values.clear()
        degraded = client.get("/api/v1/telegram/status", headers=auth)

    assert not_configured.json()["data"]["state"] == "NOT_CONFIGURED"
    assert no_allowlist.json()["data"]["state"] == "NO_ALLOWLIST"
    chat_only_data = chat_only_status.json()["data"]
    assert chat_only_data["state"] == "NO_ALLOWLIST"
    assert "user" in chat_only_data["detail"]
    assert ready.json()["data"]["state"] == "READY"
    assert degraded.json()["data"]["state"] == "DEGRADED"
    for response in (not_configured, no_allowlist, chat_only_status, ready, degraded):
        assert SENTINEL not in response.text
