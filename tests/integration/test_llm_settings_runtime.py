"""Saving settings or a key changes the next LLM call without a restart (slice 35)."""

from __future__ import annotations

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
from oft.ports.llm import ProbeOutcome

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
KEY = "sk-runtime-key-4f2a"


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


class CapturingAdapter:
    def __init__(self, seen: list[tuple[Settings, str]], models: list[str | None]) -> None:
        self._seen = seen
        self._models = models

    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        self._models.append(model)
        return ProbeOutcome(strict_schema=True, notes="strict schema accepted", latency_ms=4)


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data", **overrides)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put_settings(
    client: TestClient, auth: dict[str, str], payload: dict[str, Any], key: str
) -> Any:
    return client.put(
        "/api/v1/settings/llm",
        json=payload,
        headers={**auth, "Idempotency-Key": key},
    )


def test_a_saved_role_model_and_base_url_apply_to_the_next_probe(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    seen: list[tuple[Settings, str]] = []
    models: list[str | None] = []

    def _factory(settings: Settings, name: str) -> Any:
        seen.append((settings, name))
        return CapturingAdapter([], models)

    import oft.bootstrap as bootstrap_module

    original = bootstrap_module.build_llm_adapter
    bootstrap_module.build_llm_adapter = _factory  # type: ignore[assignment]
    try:
        app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
        auth = _auth(app)
        with TestClient(app) as client:
            _put_key = client.put(
                "/api/v1/settings/llm/openai/key",
                json={"api_key": KEY},
                headers={**auth, "Idempotency-Key": "runtime-key"},
            )
            assert _put_key.status_code == 200, _put_key.text
            saved = _put_settings(
                client,
                auth,
                {
                    "provider": "openai",
                    "roles": {"ANALYSIS": "model-a", "REVIEW": None},
                    "base_urls": {"openai": "https://api.openai.com/v1"},
                    "max_concurrency": 5,
                },
                "runtime-settings-1",
            )
            assert saved.status_code == 200, saved.text

            first = client.post(
                "/api/v1/llm/test",
                json={"provider": "openai", "role": "ANALYSIS"},
                headers=auth,
            )
            assert first.status_code == 200, first.text
            assert first.json()["data"]["status"] == "STRICT"
            assert models[-1] == "model-a"
            assert seen[-1][0].llm_openai_base_url == "https://api.openai.com/v1"
            assert seen[-1][0].llm_max_concurrency == 5

            # A second save changes the model the very next call resolves.
            _put_settings(
                client,
                auth,
                {"roles": {"ANALYSIS": "model-b"}},
                "runtime-settings-2",
            )
            second = client.post(
                "/api/v1/llm/test",
                json={"provider": "openai", "role": "ANALYSIS"},
                headers=auth,
            )
            assert second.status_code == 200, second.text
            assert models[-1] == "model-b"
    finally:
        bootstrap_module.build_llm_adapter = original  # type: ignore[assignment]


def test_deleting_the_key_is_llm_provider_not_configured_with_no_fallback(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "build_llm_adapter",
        lambda *_args: CapturingAdapter([], []),
    )
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        client.put(
            "/api/v1/settings/llm/openai/key",
            json={"api_key": KEY},
            headers={**auth, "Idempotency-Key": "delete-key-set"},
        )
        _put_settings(
            client,
            auth,
            {"provider": "openai", "roles": {"ANALYSIS": "model-a"}},
            "delete-key-settings",
        )
        assert (
            client.post(
                "/api/v1/llm/test", json={"provider": "openai", "role": "ANALYSIS"}, headers=auth
            ).status_code
            == 200
        )

        client.delete(
            "/api/v1/settings/llm/openai/key",
            headers={**auth, "Idempotency-Key": "delete-key"},
        )
        response = client.post(
            "/api/v1/llm/test", json={"provider": "openai", "role": "ANALYSIS"}, headers=auth
        )

    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "LLM_PROVIDER_NOT_CONFIGURED"


def test_a_missing_role_model_is_llm_model_not_configured(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "build_llm_adapter",
        lambda *_args: CapturingAdapter([], []),
    )
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        client.put(
            "/api/v1/settings/llm/openai/key",
            json={"api_key": KEY},
            headers={**auth, "Idempotency-Key": "no-model-key"},
        )
        _put_settings(
            client,
            auth,
            {"provider": "openai", "roles": {"ANALYSIS": None}},
            "no-model-settings",
        )
        response = client.post(
            "/api/v1/llm/test", json={"provider": "openai", "role": "ANALYSIS"}, headers=auth
        )

    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "LLM_MODEL_NOT_CONFIGURED"


def test_the_runtime_semaphore_limit_follows_the_saved_concurrency(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put_settings(
            client,
            auth,
            {"max_concurrency": 7, "max_concurrency_deepseek": 3},
            "concurrency-settings",
        )
        assert response.status_code == 200, response.text

    container = app.state.container
    container.refresh_llm_settings()
    assert container.llm_runtime.limit("openai") == 7
    assert container.llm_runtime.limit("deepseek") == 3


def test_provider_for_reuses_the_rebuilt_adapter_until_the_signature_changes(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.bootstrap as bootstrap_module

    calls: list[tuple[str | None, str]] = []

    def _factory(settings: Settings, name: str) -> Any:
        calls.append((settings.llm_provider, name))
        return CapturingAdapter([], [])

    monkeypatch.setattr(bootstrap_module, "build_llm_adapter", _factory)
    app = create_app(
        _settings(tmp_path, llm_provider="openai", openai_api_key=KEY),
        clock=FixedClock(NOW),
    )
    container = app.state.container

    first = container.provider_for()
    builds = len(calls)
    assert first is not None
    for _ in range(3):
        assert container.provider_for() is first
    assert len(calls) == builds


def test_an_unrelated_save_keeps_the_env_stub_transport_across_a_restart(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: a saved document with ``provider: null`` must not drop env ``stub``."""
    import oft.bootstrap as bootstrap_module

    resolved: list[str] = []
    original = bootstrap_module.build_llm_adapter

    def _factory(settings: Settings, name: str | None) -> Any:
        resolved.append(str(name))
        return original(settings, name)

    monkeypatch.setattr(bootstrap_module, "build_llm_adapter", _factory)
    settings = _settings(tmp_path, llm_provider="stub")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        assert app.state.container.provider_for() is not None
        assert resolved[-1] == "stub"

        saved = _put_settings(client, auth, {"max_concurrency": 5}, "stub-survives-save")
        assert saved.status_code == 200, saved.text
        # The settings surface still reports the stub transport as null.
        assert saved.json()["data"]["provider"] is None

        # The document is now user-sourced with a null provider. The environment
        # stub transport must survive the save and an explicit refresh.
        assert app.state.container.provider_for() is not None
        assert resolved[-1] == "stub"
        assert app.state.container.refresh_llm_settings().llm_provider == "stub"
        assert app.state.container.provider_for() is not None
        assert resolved[-1] == "stub"

    # A restart-style rebuild resolves the same stub transport from the
    # persisted ``provider: null`` document.
    rebuilt = bootstrap_module.build_container(settings, clock=FixedClock(NOW))
    assert rebuilt.provider_for() is not None
    assert resolved[-1] == "stub"
