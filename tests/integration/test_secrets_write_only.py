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


# -- Fix round 1: fail-closed provenance and rollback branches ----------------


def test_a_missing_stored_value_never_falls_back_to_the_env_key(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """H4: metadata says a key exists; a missing value fails closed."""
    app = create_app(_settings(tmp_path, openai_api_key=SENTINEL), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", "sk-stored-missing-9e8d").status_code == 200
        # The metadata row survives, but the backend value is gone.
        backend.values.clear()
        settings_response = client.get("/api/v1/settings/llm", headers=auth)
        providers = client.get("/api/v1/llm/providers", headers=auth)
        probe = client.post(
            "/api/v1/llm/test",
            json={"provider": "openai", "role": "ANALYSIS"},
            headers=auth,
        )

    settings_row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "openai"
    )
    providers_row = next(
        item for item in providers.json()["data"]["items"] if item["provider"] == "openai"
    )
    for row in (settings_row, providers_row):
        assert row["configured"] is False
        assert row["key_source"] == "unavailable"
        assert row["masked"] is None
    assert probe.status_code == 412, probe.text
    assert probe.json()["error"]["code"] == "LLM_PROVIDER_NOT_CONFIGURED"


def test_an_unavailable_store_fails_closed_even_with_an_env_key(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """H4: metadata + unavailable store must not leave the env key usable."""
    app = create_app(_settings(tmp_path, openai_api_key=SENTINEL), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", "sk-stored-unavailable-4f2a").status_code == 200
        backend.available = lambda: False  # type: ignore[method-assign]
        settings_response = client.get("/api/v1/settings/llm", headers=auth)
        providers = client.get("/api/v1/llm/providers", headers=auth)
        probe = client.post(
            "/api/v1/llm/test",
            json={"provider": "openai", "role": "ANALYSIS"},
            headers=auth,
        )

    settings_row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "openai"
    )
    providers_row = next(
        item for item in providers.json()["data"]["items"] if item["provider"] == "openai"
    )
    for row in (settings_row, providers_row):
        assert row["configured"] is False
        assert row["key_source"] == "unavailable"
    assert probe.status_code == 412, probe.text
    assert probe.json()["error"]["code"] == "LLM_PROVIDER_NOT_CONFIGURED"


MASTER_KEY = "unit-test-master-key-0123456789abcdef"


class _FakeKeyring:
    """In-memory OS keyring; never touches the developer's real keyring."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, reference: str) -> str | None:
        return self.values.get((service, reference))

    def set_password(self, service: str, reference: str, value: str) -> None:
        self.values[(service, reference)] = value

    def delete_password(self, service: str, reference: str) -> None:
        from oft.adapters import keyring_secrets as keyring_module

        if self.values.pop((service, reference), None) is None:
            raise keyring_module._PasswordDeleteError("absent")  # noqa: SLF001


def _install_keyring(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeKeyring, *, available: bool
) -> None:
    from oft.adapters import keyring_secrets as keyring_module

    monkeypatch.setattr(keyring_module, "_keyring", fake)
    monkeypatch.setattr(keyring_module, "keyring_available", lambda: available)


def _fake_keyring(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> _FakeKeyring:
    fake = _FakeKeyring()
    _install_keyring(monkeypatch, fake, available=available)
    return fake


def _record_meta(
    app: Any, stored: str, *, provider: str = "openai", backend: str = "unknown"
) -> None:
    from oft.domain.llm_settings import SECRET_NAME, fingerprint_secret, last4
    from oft.ports.store import SecretChangePlan, SecretMetaRecord

    def _build(_current: object) -> SecretChangePlan:
        return SecretChangePlan(
            record=SecretMetaRecord(
                provider=provider,
                name=SECRET_NAME,
                fingerprint=fingerprint_secret(stored),
                last4=last4(stored),
                updated_at="2026-11-06T10:00:00.000Z",
                updated_by="legacy",
                backend=backend,
            ),
            audit=None,
        )

    app.state.container.store.save_secret_change(provider, SECRET_NAME, _build)


def _record_unknown_meta(app: Any, stored: str) -> None:
    _record_meta(app, stored, backend="unknown")


def test_provenance_routes_reads_and_deletes_and_purges_the_old_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H7: a key stored in the file stays visible after a keyring appears."""
    from oft.application.secrets_service import EncryptedFileSecretBackend

    monkeypatch.setenv("OFT_MASTER_KEY", MASTER_KEY)
    _fake_keyring(monkeypatch, available=False)
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)
    stored = "sk-provenance-SENTINEL-7c1d"

    with TestClient(app) as client:
        assert _put_key(client, auth, "anthropic", stored, key="provenance-set").status_code == 200
        # A keyring appears after the file write; selection now prefers it.
        _fake_keyring(monkeypatch, available=True)
        providers = client.get("/api/v1/llm/providers", headers=auth)
        settings_response = client.get("/api/v1/settings/llm", headers=auth)
        deleted = client.delete(
            "/api/v1/settings/llm/anthropic/key",
            headers={**auth, "Idempotency-Key": "provenance-delete"},
        )

    providers_row = next(
        item for item in providers.json()["data"]["items"] if item["provider"] == "anthropic"
    )
    settings_row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "anthropic"
    )
    # Reads route by recorded provenance, not the newly selected keyring.
    for row in (providers_row, settings_row):
        assert row["configured"] is True
        assert row["key_source"] == "encrypted_file"
        assert row["masked"] == "****7c1d"
    assert deleted.status_code == 200, deleted.text
    # The delete removed the file ciphertext and reported the truthful state.
    file_backend = EncryptedFileSecretBackend(data_dir=settings.data_dir)
    assert file_backend.get("llm.anthropic.api_key") is None
    assert deleted.json()["data"]["configured"] is False


def test_a_legacy_unknown_row_probes_the_available_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H7: a pre-v29 ``unknown`` row is probed instead of treated as missing."""
    from oft.application.secrets_service import EncryptedFileSecretBackend

    monkeypatch.setenv("OFT_MASTER_KEY", MASTER_KEY)
    _fake_keyring(monkeypatch, available=False)
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)
    stored = "sk-legacy-unknown-7c1d"
    EncryptedFileSecretBackend(data_dir=settings.data_dir).set("llm.openai.api_key", stored)
    _record_unknown_meta(app, stored)

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/llm", headers=auth)

    row = next(
        item for item in response.json()["data"]["providers"] if item["provider"] == "openai"
    )
    assert row["configured"] is True
    assert row["masked"] == "****7c1d"
    assert row["key_source"] == "encrypted_file"


def test_an_injected_backend_never_touches_another_backend(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-C: an injected backend isolates the service from the real keyring."""
    from oft.application.secrets_service import SecretsService

    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    backend.values["llm.openai.api_key"] = SENTINEL
    _record_meta(app, SENTINEL, backend=backend.kind)
    calls: list[tuple[str, Settings | None]] = []

    def _fail(kind: str, settings_value: Settings | None) -> object:
        calls.append((kind, settings_value))
        raise AssertionError("an injected backend must not construct another backend")

    monkeypatch.setattr(secrets_module, "backend_for_kind", _fail)
    # Monkeypatched ``default_backend`` (the fixture) is also isolated.
    from_default = SecretsService(
        store=app.state.container.store, settings=settings, clock=FixedClock(NOW)
    )
    assert from_default.resolve_key("openai") == SENTINEL
    from_default.delete_key("openai", actor="test")
    # Explicit constructor injection is isolated too.
    backend.values["llm.openai.api_key"] = SENTINEL
    _record_meta(app, SENTINEL, backend=backend.kind)
    injected = SecretsService(
        store=app.state.container.store,
        settings=settings,
        clock=FixedClock(NOW),
        backend=backend,
    )
    assert injected.status("openai").configured is True
    injected.delete_key("openai", actor="test")
    assert calls == []


def test_a_legacy_unknown_row_with_no_value_is_not_falsely_revoked(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """H-B(a): no readable value means no delete and no SECRET_DELETED audit."""
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)
    _record_unknown_meta(app, "sk-missing-legacy-4f2a")

    with TestClient(app) as client:
        deleted = client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**auth, "Idempotency-Key": "legacy-missing"},
        )

    assert deleted.status_code == 500, deleted.text
    assert deleted.json()["error"]["code"] == "CONFIG_INVALID"
    assert app.state.container.store.get_secret_meta("openai") is not None
    assert app.state.container.store.list_audit_events() == ()


def test_a_recorded_backend_that_is_unavailable_does_not_audit_a_revoke(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """H-B(b): a lost backend must not produce a false revocation."""
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)
    with TestClient(app) as client:
        assert _put_key(client, auth, "openai", SENTINEL, key="lost-backend-set").status_code == 200
        backend.available = lambda: False  # type: ignore[method-assign]
        deleted = client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**auth, "Idempotency-Key": "lost-backend-delete"},
        )

    assert deleted.status_code == 500, deleted.text
    assert deleted.json()["error"]["code"] == "CONFIG_INVALID"
    assert app.state.container.store.get_secret_meta("openai") is not None
    events = [event.event_type for event in app.state.container.store.list_audit_events()]
    assert events == ["SECRET_ADDED"]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    info = _record
    warning = _record
    error = _record


def test_rotation_purges_superseded_backend_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-F: keyring -> file rotation logs residue; a later rotation cleans it."""
    from oft.application.secrets_service import EncryptedFileSecretBackend, SecretsService

    monkeypatch.setenv("OFT_MASTER_KEY", MASTER_KEY)
    fake = _fake_keyring(monkeypatch, available=True)
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    store = app.state.container.store
    reference = "llm.openai.api_key"
    first = "sk-rotation-keyring-1111"
    second = "sk-rotation-file-2222"
    third = "sk-rotation-keyring-3333"

    SecretsService(store=store, settings=settings, clock=FixedClock(NOW)).set_key(
        "openai", first, actor="test"
    )
    assert fake.values[("oft", reference)] == first

    # The keyring disappears; rotate into the encrypted file. The superseded
    # keyring value cannot be purged, so the residue is logged.
    recording = _Recorder()
    monkeypatch.setattr(secrets_module, "LOGGER", recording)
    _install_keyring(monkeypatch, fake, available=False)
    SecretsService(store=store, settings=settings, clock=FixedClock(NOW)).set_key(
        "openai", second, actor="test"
    )
    file_backend = EncryptedFileSecretBackend(data_dir=settings.data_dir)
    assert file_backend.get(reference) == second
    assert any(event == "secret_rotation_residue_unavailable" for event, _ in recording.events)

    # The keyring returns; the next rotation back to it removes the file copy.
    _install_keyring(monkeypatch, fake, available=True)
    SecretsService(store=store, settings=settings, clock=FixedClock(NOW)).set_key(
        "openai", third, actor="test"
    )
    assert fake.values[("oft", reference)] == third
    assert file_backend.get(reference) is None


def test_a_metadata_failure_rolls_back_a_newly_stored_key(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: a metadata write failure never leaves the backend ahead."""
    from oft.application.secrets_service import SecretsService

    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    service = SecretsService(
        store=app.state.container.store, settings=settings, clock=FixedClock(NOW)
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(app.state.container.store, "save_secret_change", _boom)
    with pytest.raises(RuntimeError):
        service.set_key("openai", SENTINEL, actor="web")
    assert backend.values == {}


def test_a_metadata_failure_restores_the_previous_key_on_rotation(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: a failed rotation restores the previous backend value."""
    from oft.application.secrets_service import SecretsService

    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    service = SecretsService(
        store=app.state.container.store, settings=settings, clock=FixedClock(NOW)
    )
    first = "sk-rollback-first-1111"
    second = "sk-rollback-second-2222"
    service.set_key("openai", first, actor="web")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(app.state.container.store, "save_secret_change", _boom)
    with pytest.raises(RuntimeError):
        service.set_key("openai", second, actor="web")
    assert backend.values["llm.openai.api_key"] == first


def test_a_metadata_failure_restores_a_deleted_key(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: a failed delete restores the value instead of claiming revocation."""
    from oft.application.secrets_service import SecretsService

    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    service = SecretsService(
        store=app.state.container.store, settings=settings, clock=FixedClock(NOW)
    )
    stored = "sk-delete-rollback-4f2a"
    service.set_key("openai", stored, actor="web")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(app.state.container.store, "save_secret_change", _boom)
    with pytest.raises(RuntimeError):
        service.delete_key("openai", actor="web")
    assert backend.values["llm.openai.api_key"] == stored


def test_an_overlong_key_is_422_and_the_value_is_never_echoed(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """M6: the Pydantic max-length refusal never repeats the sentinel."""
    overlong = "sk-SENTINEL-" + "a" * 600
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put_key(client, _auth(app), "openai", overlong, key="too-long")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert overlong not in response.text
    assert overlong[:24] not in response.text
    assert app.state.container.store.list_secret_meta() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_a_short_master_key_without_a_keyring_does_not_crash_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: selection records a reason; only a store write raises it."""
    from oft.adapters import keyring_secrets as keyring_module
    from oft.bootstrap import build_container

    monkeypatch.setenv("OFT_MASTER_KEY", "too-short")
    monkeypatch.setattr(keyring_module, "keyring_available", lambda: False)
    settings = _settings(tmp_path)

    container = build_container(settings, clock=FixedClock(NOW))
    assert container.settings is not None

    app = create_app(settings, clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = _put_key(client, _auth(app), "openai", SENTINEL, key="short-master")

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["code"] == "CONFIG_INVALID"
    assert "at least 32" in error["message"].lower()
    assert SENTINEL not in response.text
    assert not (settings.data_dir / "secrets.enc").exists()


def test_a_refresh_failure_after_a_committed_key_write_still_returns_200(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the post-commit refresh is best effort and never re-rotates."""
    from oft.bootstrap import AppContainer

    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)
    container = app.state.container

    def _fail(_self: AppContainer) -> None:
        raise RuntimeError("refresh down")

    monkeypatch.setattr(AppContainer, "refresh_llm_settings", _fail)
    with TestClient(app) as client:
        first = _put_key(client, auth, "openai", SENTINEL, key="refresh-failure")
        replay = _put_key(client, auth, "openai", SENTINEL, key="refresh-failure")

    assert first.status_code == 200, first.text
    assert first.json()["data"]["masked"] == "****4f2a"
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["idempotent_replay"] is True
    # One commit, one audit event, one backend value: no duplicate rotation.
    audits = [event.event_type for event in container.store.list_audit_events()]
    assert audits == ["SECRET_ADDED"]
    assert backend.values["llm.openai.api_key"] == SENTINEL
