"""Onboarding step 4 (Telegram) is a real optional step (slice 53, S01.4)."""

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
from oft.application.settings_service import ONBOARDING_PROGRESS_KEY
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _app(tmp_path: Path, **overrides: Any) -> Any:
    return create_app(
        Settings(
            env="test",
            port=8765,
            data_dir=tmp_path / "data",
            openai_api_key="sk-test-onboarding-telegram",
            **overrides,
        ),
        clock=FixedClock(NOW),
    )


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put_step(client: TestClient, app: Any, step: int, key: str) -> Any:
    return client.put(
        "/api/v1/settings/onboarding",
        json={"step": step},
        headers={**_auth(app), "Idempotency-Key": key},
    )


def _configure_telegram(client: TestClient, app: Any) -> None:
    token = client.put(
        "/api/v1/settings/telegram/token",
        json={"bot_token": SENTINEL},
        headers={**_auth(app), "Idempotency-Key": "tg-token"},
    )
    assert token.status_code == 200, token.text
    settings = client.put(
        "/api/v1/settings/telegram",
        json={"chat_ids": [1001], "allowed_user_ids": [2002]},
        headers={**_auth(app), "Idempotency-Key": "tg-settings"},
    )
    assert settings.status_code == 200, settings.text


def _complete_required_steps(client: TestClient, app: Any) -> None:
    star = client.post("/api/v1/watchlist/BTCUSDT", headers=_auth(app))
    assert star.status_code == 200, star.text
    for index, step in enumerate((1, 2, 5)):
        response = _put_step(client, app, step, f"step-{index}")
        assert response.status_code == 200, response.text


def test_step_4_is_active_optional_and_not_deferred(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        data = client.get("/api/v1/settings/onboarding", headers=_auth(app)).json()["data"]

    step = next(item for item in data["steps"] if item["step"] == 4)
    assert step == {
        "step": 4,
        "id": "telegram",
        "title": "Telegram 只读通道",
        "deferred": False,
        "deferred_milestone": None,
        "optional": True,
        "completed": False,
    }
    # Step 3 Binance stays deferred.
    binance = next(item for item in data["steps"] if item["step"] == 3)
    assert binance["deferred"] is True
    assert binance["deferred_milestone"] == 5
    assert binance["optional"] is False


def test_step_4_without_a_token_or_chat_is_rejected_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        no_token = _put_step(client, app, 4, "no-token")
        assert no_token.status_code == 422, no_token.text
        assert no_token.json()["error"]["details"][0]["code"] == "step_prerequisite_missing"

        token = client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers={**_auth(app), "Idempotency-Key": "tg-token"},
        )
        assert token.status_code == 200
        no_chat = _put_step(client, app, 4, "no-chat")

    assert no_chat.status_code == 422, no_chat.text
    error = no_chat.json()["error"]
    assert error["details"][0]["code"] == "step_prerequisite_missing"
    assert "chat" in error["message"].lower()
    assert app.state.container.store.get_config(ONBOARDING_PROGRESS_KEY) is None


def test_step_4_completes_only_with_a_token_and_a_chat(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _configure_telegram(client, app)
        response = _put_step(client, app, 4, "step-4")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["completed_steps"] == [4]
    assert data["all_complete"] is False
    assert data["resumed_step"] == 1
    row = app.state.container.store.get_config(ONBOARDING_PROGRESS_KEY)
    assert row is not None
    assert json.loads(row.value_json) == {"completed_steps": [4]}


def test_an_incomplete_optional_step_does_not_block_all_complete_but_resumes_at_4(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _complete_required_steps(client, app)
        data = client.get("/api/v1/settings/onboarding", headers=_auth(app)).json()["data"]

    assert data["completed_steps"] == [1, 2, 5]
    assert data["all_complete"] is True
    assert data["resumed_step"] == 4


def test_completing_step_4_after_the_required_steps_moves_resume_to_5(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        _complete_required_steps(client, app)
        _configure_telegram(client, app)
        response = _put_step(client, app, 4, "step-4")
        data = response.json()["data"]

    assert response.status_code == 200, response.text
    assert data["completed_steps"] == [1, 2, 4, 5]
    assert data["all_complete"] is True
    assert data["resumed_step"] == 5


def test_step_3_remains_deferred_and_writes_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        response = _put_step(client, app, 3, "step-3")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["code"] == "step_deferred"
    assert app.state.container.store.get_config(ONBOARDING_PROGRESS_KEY) is None


def test_a_hand_seeded_deferred_step_is_dropped_but_step_4_is_kept(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    app = _app(tmp_path)
    app.state.container.store.set_config(
        ConfigRecord(
            key=ONBOARDING_PROGRESS_KEY,
            value_json=json.dumps({"completed_steps": [3, 4]}),
            updated_at="2026-11-18T10:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        data = client.get("/api/v1/settings/onboarding", headers=_auth(app)).json()["data"]

    assert data["completed_steps"] == [4]
