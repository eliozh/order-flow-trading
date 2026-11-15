"""``/api/v1/settings/llm`` read/write contract and validation (slice 35)."""

from __future__ import annotations

import json
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
PROVIDERS = ("openai", "anthropic", "deepseek")
ROLES = ("ANALYSIS", "ENTRY_RECHECK", "MANAGEMENT", "REVIEW")


class MemorySecretBackend(SecretBackend):
    kind = "encrypted_file"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

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


def _put(client: TestClient, auth: dict[str, str], payload: dict[str, Any], key: str) -> Any:
    return client.put(
        "/api/v1/settings/llm",
        json=payload,
        headers={**auth, "Idempotency-Key": key},
    )


def test_get_defaults_lists_three_provider_rows_and_env_roles(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    settings = _settings(
        tmp_path,
        llm_provider="openai",
        llm_model_analysis="gpt-default",
        llm_openai_base_url="https://api.openai.com/v1",
    )
    app = create_app(settings, clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/llm", headers=_auth(app))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["provider"] == "openai"
    assert data["source"] == "default"
    assert data["updated_at"] is None
    assert data["updated_by"] is None
    assert tuple(data["roles"]) == ROLES
    assert data["roles"]["ANALYSIS"] == "gpt-default"
    assert data["roles"]["REVIEW"] is None
    assert data["base_urls"] == {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "deepseek": "https://api.deepseek.com",
    }
    assert data["allow_custom_base_url"] is False
    assert data["concurrency"] == {"global": 2, "deepseek": 1}
    assert [row["provider"] for row in data["providers"]] == list(PROVIDERS)
    for row in data["providers"]:
        assert set(row) == {
            "provider",
            "configured",
            "key_source",
            "masked",
            "fingerprint",
            "updated_at",
            "updated_by",
            "base_url",
            "capability",
        }
        assert row["configured"] is False
        assert row["capability"] is None


def test_put_persists_the_effective_document_and_audits_one_event(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put(
            client,
            auth,
            {
                "provider": "anthropic",
                "roles": {"ANALYSIS": "claude-a", "MANAGEMENT": "claude-m"},
                "base_urls": {"anthropic": "https://api.anthropic.com"},
                "allow_custom_base_url": True,
                "max_concurrency": 4,
                "max_concurrency_deepseek": 2,
            },
            "llm-save-1",
        )
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["provider"] == "anthropic"
    assert data["source"] == "user"
    assert data["roles"]["ANALYSIS"] == "claude-a"
    assert data["roles"]["MANAGEMENT"] == "claude-m"
    assert data["roles"]["ENTRY_RECHECK"] is None
    assert data["allow_custom_base_url"] is True
    assert data["concurrency"] == {"global": 4, "deepseek": 2}
    assert data["updated_at"] == "2026-11-06T10:00:00.000Z"
    assert data["updated_by"] == "web"
    assert fetched.json()["data"] == data

    audits = app.state.container.store.list_audit_events()
    assert [event.event_type for event in audits] == ["CONFIG_UPDATED"]
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["key"] == "llm.settings"
    assert set(payload) >= {"key", "before", "after", "reason"}
    assert not any(marker in audits[0].payload_json.lower() for marker in ("api_key", "secret"))


def test_unchanged_save_is_a_no_op_with_no_new_audit(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"max_concurrency": 4}, "noop-1")
        assert first.status_code == 200, first.text
        before = app.state.container.store.list_audit_events()
        second = _put(client, auth, {"max_concurrency": 4}, "noop-2")

    assert second.status_code == 200, second.text
    assert second.json()["meta"]["idempotent_replay"] is False
    assert app.state.container.store.list_audit_events() == before


def test_unofficial_base_url_reports_the_rule_and_field_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(
            client,
            _auth(app),
            {"base_urls": {"anthropic": "https://api.anthropic.com.evil.example"}},
            "bad-host",
        )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    detail = error["details"][0]
    assert detail["field"] == "base_urls.anthropic"
    assert detail["code"] == "unofficial_host"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://api.openai.com", "https_required"),
        ("https://user:pass@api.openai.com", "userinfo_forbidden"),
        ("https://api.openai.com?a=1", "query_forbidden"),
        ("https://api.openai.com#f", "fragment_forbidden"),
    ],
)
def test_base_url_rules_reject_and_name_the_rule(
    tmp_path: Path, backend: MemorySecretBackend, url: str, code: str
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(
            client,
            _auth(app),
            {"base_urls": {"openai": url}},
            f"bad-{code}",
        )

    assert response.status_code == 422, response.text
    detail = response.json()["error"]["details"][0]
    assert detail["field"] == "base_urls.openai"
    assert detail["code"] == code


def test_custom_base_url_requires_the_explicit_opt_in(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        refused = _put(
            client,
            auth,
            {"base_urls": {"openai": "https://gateway.example.com"}},
            "custom-refused",
        )
        accepted = _put(
            client,
            auth,
            {
                "base_urls": {"openai": "https://gateway.example.com"},
                "allow_custom_base_url": True,
            },
            "custom-accepted",
        )

    assert refused.status_code == 422
    assert refused.json()["error"]["details"][0]["code"] == "unofficial_host"
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["base_urls"]["openai"] == "https://gateway.example.com"


def test_invalid_provider_role_and_concurrency_are_validation_failures(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        bad_provider = _put(client, auth, {"provider": "stub"}, "bad-provider")
        bad_role = _put(client, auth, {"roles": {"TEMPERATURE": "1"}}, "bad-role")
        bad_concurrency = _put(client, auth, {"max_concurrency": 0}, "bad-concurrency")

    for response in (bad_provider, bad_role, bad_concurrency):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_config() == ()


def test_put_requires_an_idempotency_key(tmp_path: Path, backend: MemorySecretBackend) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/llm",
            json={"max_concurrency": 3},
            headers=_auth(app),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_same_key_with_a_different_payload_is_a_conflict(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert _put(client, auth, {"max_concurrency": 3}, "conflict").status_code == 200
        conflict = _put(client, auth, {"max_concurrency": 4}, "conflict")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_corrupt_persisted_llm_document_is_config_invalid(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    from oft.ports.store import ConfigRecord

    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_config(
        ConfigRecord(
            key="llm.settings",
            value_json="not-json",
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/llm", headers=_auth(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_secrets_meta_shape_is_identical_for_a_fresh_and_a_migrated_database(
    tmp_path: Path,
) -> None:
    import sqlite3

    from oft.adapters.sqlite_schema import initialize_database

    fresh_settings = Settings(env="test", port=8765, data_dir=tmp_path / "fresh")
    initialize_database(fresh_settings)
    fresh = sqlite3.connect(fresh_settings.data_dir / "oft.db")
    try:
        fresh_shape = fresh.execute("PRAGMA table_info(secrets_meta)").fetchall()
    finally:
        fresh.close()

    migrated_settings = Settings(env="test", port=8765, data_dir=tmp_path / "migrated")
    initialize_database(migrated_settings)
    migrated = sqlite3.connect(migrated_settings.data_dir / "oft.db")
    try:
        # Simulate a v27 database: remove both the table and its version rows,
        # then let the v28/v29 migrations recreate the documented shape.
        migrated.execute("DROP TABLE secrets_meta")
        migrated.execute("DELETE FROM schema_migrations WHERE version IN (28, 29)")
        migrated.commit()
    finally:
        migrated.close()
    initialize_database(migrated_settings)
    migrated = sqlite3.connect(migrated_settings.data_dir / "oft.db")
    try:
        migrated_shape = migrated.execute("PRAGMA table_info(secrets_meta)").fetchall()
        version = migrated.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        migrated.close()

    assert fresh_shape == migrated_shape
    assert [row[1] for row in fresh_shape] == [
        "provider",
        "name",
        "fingerprint",
        "last4",
        "updated_at",
        "updated_by",
        "backend",
    ]
    assert version == 29


def test_disabling_the_custom_opt_in_with_a_custom_url_is_rejected(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        accepted = _put(
            client,
            auth,
            {
                "base_urls": {"openai": "https://gateway.example.com"},
                "allow_custom_base_url": True,
            },
            "custom-on",
        )
        assert accepted.status_code == 200, accepted.text
        # A probe/scan refresh overlays the persisted custom URL on the shared
        # settings; the disable guard must still reject it.
        app.state.container.refresh_llm_settings()
        refused = _put(
            client,
            auth,
            {"allow_custom_base_url": False},
            "custom-off",
        )
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert refused.status_code == 422, refused.text
    detail = refused.json()["error"]["details"][0]
    assert detail["field"] == "base_urls.openai"
    assert detail["code"] == "unofficial_host"
    # The accepted document remains readable and unchanged.
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["base_urls"]["openai"] == "https://gateway.example.com"
    assert fetched.json()["data"]["allow_custom_base_url"] is True


def test_disabling_the_opt_in_with_an_env_custom_url_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    settings = _settings(
        tmp_path,
        llm_openai_base_url="https://gateway.example.com",
        llm_allow_custom_base_url=True,
    )
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        refused = _put(client, auth, {"allow_custom_base_url": False}, "env-custom-off")
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"][0]["code"] == "unofficial_host"
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["base_urls"]["openai"] == "https://gateway.example.com"
    assert fetched.json()["data"]["allow_custom_base_url"] is True
    # Nothing was committed, so a fresh container still builds and reads.
    from oft.bootstrap import build_container

    container = build_container(settings, clock=FixedClock(NOW))
    assert container.settings.llm_allow_custom_base_url is True


def test_an_env_stub_provider_is_reported_as_null_and_never_persisted(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path, llm_provider="stub"), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        fetched = client.get("/api/v1/settings/llm", headers=auth)
        saved = _put(client, auth, {"max_concurrency": 3}, "stub-save")
        after = client.get("/api/v1/settings/llm", headers=auth)

    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["provider"] is None
    assert saved.status_code == 200, saved.text
    assert after.status_code == 200, after.text
    assert after.json()["data"]["provider"] is None
    persisted = app.state.container.store.get_config("llm.settings")
    assert persisted is not None
    assert json.loads(persisted.value_json)["provider"] is None


def test_an_invalid_env_inherited_field_refuses_the_save_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """H1: the final document is validated with the read-path validator.

    An invalid environment-inherited role model must make the save a 422 and
    write nothing, never persist a document that makes every later read
    ``CONFIG_INVALID``. The environment document itself stays readable.
    """
    settings = _settings(tmp_path, llm_model_analysis="bad model")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        refused = _put(client, auth, {"max_concurrency": 3}, "invalid-env-save")
        fetched = client.get("/api/v1/settings/llm", headers=auth)

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "roles.ANALYSIS"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["roles"]["ANALYSIS"] == "bad model"


def test_a_saved_role_model_appears_in_the_models_catalog(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """M7/M-K: models read the persisted overlay, not the startup snapshot."""
    from oft.application.settings_service import SettingsService

    app = create_app(_settings(tmp_path, llm_provider="openai"), clock=FixedClock(NOW))
    auth = _auth(app)
    # Persist directly so the runtime snapshot stays stale; only a refresh in
    # the models route can make this test pass.
    base = app.state.container.llm_base_settings or app.state.container.settings
    SettingsService(
        store=app.state.container.store,
        clock=app.state.container.clock,
        settings=base,
    ).update_llm_settings(
        {"provider": "openai", "roles": {"ANALYSIS": "gpt-persisted"}},
        actor="test",
    )
    assert "gpt-persisted" not in (app.state.container.settings.llm_model_analysis or "")

    with TestClient(app) as client:
        models = client.get("/api/v1/llm/models?provider=openai", headers=auth)

    assert models.status_code == 200, models.text
    names = [item["model"] for item in models.json()["data"]["items"]]
    assert "gpt-persisted" in names


def test_a_refresh_failure_after_a_committed_document_write_returns_200(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: a committed document write is never reported as a failed 500."""
    from oft.bootstrap import AppContainer

    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    def _fail(_self: AppContainer) -> None:
        raise RuntimeError("refresh down")

    monkeypatch.setattr(AppContainer, "refresh_llm_settings", _fail)
    with TestClient(app) as client:
        first = _put(client, auth, {"max_concurrency": 4}, "refresh-doc")
        replay = _put(client, auth, {"max_concurrency": 4}, "refresh-doc")

    assert first.status_code == 200, first.text
    assert first.json()["data"]["concurrency"]["global"] == 4
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert [event.event_type for event in app.state.container.store.list_audit_events()] == [
        "CONFIG_UPDATED"
    ]


def test_providers_route_reports_the_stored_mask_after_a_key_save(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/settings/llm/openai/key",
            json={"api_key": "sk-providers-mask-4f2a"},
            headers={**auth, "Idempotency-Key": "providers-mask"},
        )
        providers = client.get("/api/v1/llm/providers", headers=auth)

    assert saved.status_code == 200, saved.text
    assert providers.status_code == 200, providers.text
    row = next(item for item in providers.json()["data"]["items"] if item["provider"] == "openai")
    assert row["configured"] is True
    # Same provenance the settings route and CLI report for a stored key.
    assert row["key_source"] == "encrypted_file"
    assert row["masked"] == "****4f2a"


def test_providers_route_reports_unavailable_when_the_store_is_unreadable(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """N2: metadata plus an unreadable store is ``unavailable``, never stored.

    An environment key must not disguise the unavailable store as a stored key:
    the providers route and ``/api/v1/settings/llm`` must report one truth.
    """
    app = create_app(
        _settings(tmp_path, openai_api_key="sk-env-fallback-4f2a"), clock=FixedClock(NOW)
    )
    auth = _auth(app)

    with TestClient(app) as client:
        seeded = client.put(
            "/api/v1/settings/llm/openai/key",
            json={"api_key": "sk-providers-unavailable-9e8d"},
            headers={**auth, "Idempotency-Key": "providers-unavailable-set"},
        )
        assert seeded.status_code == 200, seeded.text
        # The metadata row remains, but the backend is no longer readable.
        backend.available = lambda: False  # type: ignore[method-assign]
        providers = client.get("/api/v1/llm/providers", headers=auth)
        settings_response = client.get("/api/v1/settings/llm", headers=auth)

    assert providers.status_code == 200, providers.text
    row = next(item for item in providers.json()["data"]["items"] if item["provider"] == "openai")
    settings_row = next(
        item
        for item in settings_response.json()["data"]["providers"]
        if item["provider"] == "openai"
    )
    assert row["configured"] is False
    assert row["key_source"] == "unavailable"
    assert row["masked"] is None
    assert settings_row["configured"] is False
    assert settings_row["key_source"] == "unavailable"
    assert settings_row["masked"] is None
