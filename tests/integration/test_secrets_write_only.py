"""Provider keys are write-only across every surface (slice 35).

The same sentinel is checked in the response JSON, audit payloads, SQLite file
bytes, the encrypted store bytes, captured logs, and error bodies. A key may
only ever surface as the fixed mask plus its last four characters.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.application.secrets_service import SecretBackend
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
SENTINEL = "sk-SENTINEL-DO-NOT-LEAK-4f2a"


class MemorySecretBackend(SecretBackend):
    """Deterministic stand-in for the OS keyring / encrypted-file adapter."""

    kind = "encrypted_file"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.kind = "encrypted_file"

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data", **overrides)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put_key(
    client: TestClient, auth: dict[str, str], provider: str, value: str, key: str = "key-1"
) -> Any:
    return client.put(
        f"/api/v1/settings/llm/{provider}/key",
        json={"api_key": value},
        headers={**auth, "Idempotency-Key": key},
    )


def test_key_add_returns_only_the_mask_and_never_the_value(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put_key(client, _auth(app), "anthropic", SENTINEL)
        settings_response = client.get("/api/v1/settings/llm", headers=_auth(app))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {
        "provider": "anthropic",
        "configured": True,
        "key_source": "encrypted_file",
        "masked": "****4f2a",
        "fingerprint": data["fingerprint"],
        "updated_at": "2026-11-06T10:00:00.000Z",
        "updated_by": "web",
    }
    assert len(data["fingerprint"]) == 64
    assert SENTINEL not in response.text
    assert SENTINEL[:12] not in response.text
    assert SENTINEL not in settings_response.text
    assert SENTINEL[:12] not in settings_response.text

    row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "anthropic"
    )
    assert row["configured"] is True
    assert row["masked"] == "****4f2a"
    assert row["key_source"] == "encrypted_file"
    assert row["fingerprint"] == data["fingerprint"]

    audits = app.state.container.store.list_audit_events()
    assert [event.event_type for event in audits] == ["SECRET_ADDED"]
    assert SENTINEL not in audits[0].payload_json
    assert SENTINEL not in json.dumps(settings_response.json())

    # Neither the SQLite file nor the backend state contains the plaintext key.
    database = app.state.container.store.path
    with sqlite3.connect(database) as connection:
        meta = connection.execute(
            "SELECT provider, name, fingerprint, last4 FROM secrets_meta"
        ).fetchall()
    assert meta == [("anthropic", "api_key", data["fingerprint"], "4f2a")]
    assert SENTINEL.encode() not in database.read_bytes()


def test_rotation_writes_a_rotated_event_and_moves_the_mask(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put_key(client, auth, "openai", "sk-first-key-1111", key="rotate-1")
        second = _put_key(client, auth, "openai", "sk-second-key-2222", key="rotate-2")

    assert first.json()["data"]["masked"] == "****1111"
    assert second.json()["data"]["masked"] == "****2222"
    audits = [event.event_type for event in app.state.container.store.list_audit_events()]
    assert sorted(audits) == ["SECRET_ADDED", "SECRET_ROTATED"]
    assert "sk-first-key-1111" not in "\n".join(
        event.payload_json for event in app.state.container.store.list_audit_events()
    )


def test_delete_removes_the_key_and_the_provider_becomes_unconfigured(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        _put_key(client, auth, "deepseek", "sk-deepseek-key-9b71")
        deleted = client.delete(
            "/api/v1/settings/llm/deepseek/key",
            headers={**auth, "Idempotency-Key": "delete-1"},
        )
        settings_response = client.get("/api/v1/settings/llm", headers=auth)

    assert deleted.status_code == 200, deleted.text
    data = deleted.json()["data"]
    assert data["configured"] is False
    assert data["key_source"] == "unset"
    assert data["masked"] is None
    assert data["fingerprint"] is None
    row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "deepseek"
    )
    assert row["configured"] is False
    assert row["key_source"] == "unset"
    audits = [event.event_type for event in app.state.container.store.list_audit_events()]
    assert sorted(audits) == ["SECRET_ADDED", "SECRET_DELETED"]
    assert app.state.container.store.list_secret_meta() == ()


def test_environment_keys_report_env_without_a_last4(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path, openai_api_key=SENTINEL), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/llm", headers=auth)

    row = next(
        item for item in response.json()["data"]["providers"] if item["provider"] == "openai"
    )
    assert row["configured"] is True
    assert row["key_source"] == "env"
    assert row["masked"] is None
    assert row["fingerprint"] is None
    assert SENTINEL not in response.text


def test_without_a_secure_store_the_write_is_refused_and_no_plaintext_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: None)
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put_key(client, _auth(app), "openai", SENTINEL)

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["code"] == "CONFIG_INVALID"
    assert "secure secret store" in error["message"].lower()
    assert SENTINEL not in response.text
    assert not (settings.data_dir / "secrets.enc").exists()
    assert app.state.container.store.list_secret_meta() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_the_key_never_appears_in_captured_logs(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []

    class _Recorder:
        def info(self, event: str, **fields: Any) -> None:
            messages.append(f"{event} {fields}")

        warning = info
        error = info
        exception = info

    monkeypatch.setattr(secrets_module, "LOGGER", _Recorder())
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        _put_key(client, _auth(app), "openai", SENTINEL)

    assert messages
    assert SENTINEL not in "\n".join(messages)


def test_invalid_key_shapes_are_validation_failures_and_write_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put_key(client, _auth(app), "openai", "   ", key="blank-key")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "api_key"
    assert app.state.container.store.list_secret_meta() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_the_encrypted_file_fallback_never_writes_plaintext_and_is_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application.secrets_service import EncryptedFileSecretBackend

    master = "unit-test-master-key-0123456789abcdef"
    monkeypatch.setenv("OFT_MASTER_KEY", master)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    backend = EncryptedFileSecretBackend(data_dir=data_dir)

    assert backend.available() is True
    backend.set("llm.openai.api_key", SENTINEL)
    path = data_dir / "secrets.enc"

    assert path.exists()
    raw = path.read_bytes()
    assert SENTINEL.encode() not in raw
    assert SENTINEL[:10].encode() not in raw
    assert (path.stat().st_mode & 0o777) == 0o600
    assert backend.get("llm.openai.api_key") == SENTINEL
    backend.delete("llm.openai.api_key")
    assert backend.get("llm.openai.api_key") is None


def test_the_encrypted_file_fallback_is_unavailable_without_a_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application.secrets_service import EncryptedFileSecretBackend

    monkeypatch.delenv("OFT_MASTER_KEY", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    backend = EncryptedFileSecretBackend(data_dir=data_dir)

    assert backend.available() is False


def test_refresh_then_delete_reports_the_provider_unconfigured(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """F1: a runtime refresh must not turn a deleted stored key into an env key."""
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", SENTINEL, key="refresh-set").status_code == 200
        # A probe/scan refresh writes the stored key into the shared snapshot.
        app.state.container.refresh_llm_settings()
        deleted = client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**auth, "Idempotency-Key": "refresh-delete"},
        )
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["configured"] is False
    assert deleted.json()["data"]["key_source"] == "unset"
    assert deleted.json()["data"]["masked"] is None
    row = next(item for item in fetched.json()["data"]["providers"] if item["provider"] == "openai")
    assert row["configured"] is False
    assert row["key_source"] == "unset"
    assert row["masked"] is None


def test_deleting_the_stored_key_keeps_a_real_environment_key_in_effect(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    stored = "sk-stored-rotation-4f2a"
    app = create_app(_settings(tmp_path, openai_api_key=SENTINEL), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", stored, key="env-set").status_code == 200
        app.state.container.refresh_llm_settings()
        deleted = client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**auth, "Idempotency-Key": "env-delete"},
        )
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert deleted.status_code == 200, deleted.text
    data = deleted.json()["data"]
    assert data["configured"] is True
    assert data["key_source"] == "env"
    assert data["masked"] is None
    row = next(item for item in fetched.json()["data"]["providers"] if item["provider"] == "openai")
    assert row["configured"] is True
    assert row["key_source"] == "env"
    assert SENTINEL not in fetched.text


def test_delete_revokes_an_orphaned_backend_value_without_metadata(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    backend.values["llm.openai.api_key"] = SENTINEL
    assert app.state.container.store.list_secret_meta() == ()

    with TestClient(app) as client:
        deleted = client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**_auth(app), "Idempotency-Key": "orphan-delete"},
        )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["configured"] is False
    assert backend.values == {}
    assert app.state.container.store.list_audit_events() == ()


def test_concurrent_encrypted_file_writers_never_lose_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from oft.application.secrets_service import EncryptedFileSecretBackend

    master = "unit-test-master-key-0123456789abcdef"
    monkeypatch.setenv("OFT_MASTER_KEY", master)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    backend = EncryptedFileSecretBackend(data_dir=data_dir)
    references = [f"llm.provider{i}.api_key" for i in range(8)]

    def writer(reference: str) -> None:
        backend.set(reference, f"sk-concurrent-{reference}-4f2a")

    threads = [threading.Thread(target=writer, args=(reference,)) for reference in references]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    values = backend._load()  # noqa: SLF001 - the test asserts the stored map
    assert set(values) == set(references)


def test_a_short_master_key_is_refused_with_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application.secrets_service import (
        EncryptedFileSecretBackend,
        SecretStoreUnavailable,
    )

    monkeypatch.setenv("OFT_MASTER_KEY", "too-short")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    backend = EncryptedFileSecretBackend(data_dir=data_dir)

    assert backend.available() is False
    with pytest.raises(SecretStoreUnavailable) as excinfo:
        backend.set("llm.openai.api_key", SENTINEL)
    assert "at least 32" in excinfo.value.message


def test_the_decrypted_map_is_cached_between_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import secrets_service as module

    master = "unit-test-master-key-0123456789abcdef"
    monkeypatch.setenv("OFT_MASTER_KEY", master)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    backend = module.EncryptedFileSecretBackend(data_dir=data_dir)
    calls: list[int] = []
    original = module._derive_fernet_key

    def counting(master_value: str, salt: bytes) -> bytes:
        calls.append(1)
        return original(master_value, salt)

    monkeypatch.setattr(module, "_derive_fernet_key", counting)
    backend.set("llm.openai.api_key", SENTINEL)
    derived_after_write = len(calls)
    assert backend.get("llm.openai.api_key") == SENTINEL
    derived_after_first_read = len(calls)
    assert backend.get("llm.openai.api_key") == SENTINEL
    assert derived_after_first_read == derived_after_write + 1
    assert len(calls) == derived_after_first_read


def test_an_unreadable_store_reports_unavailable_not_unset(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", SENTINEL, key="unavailable-set").status_code == 200
        # Simulate a metadata row whose backend became unavailable (a rotated
        # or mistyped master key, an unreadable keyring).
        backend.available = lambda: False  # type: ignore[method-assign]
        response = client.get("/api/v1/settings/llm", headers=auth)

    assert response.status_code == 200, response.text
    row = next(
        item for item in response.json()["data"]["providers"] if item["provider"] == "openai"
    )
    assert row["configured"] is False
    assert row["key_source"] == "unavailable"
    assert row["masked"] is None
