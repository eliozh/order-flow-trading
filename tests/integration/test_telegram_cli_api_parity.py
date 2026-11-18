"""API and CLI share one Telegram settings document (slice 53, M-11).

Both surfaces go through :class:`~oft.application.settings_service.SettingsService`
against the same data directory. These tests prove a write on one surface is
readable on the other, rather than each surface keeping a private copy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from fakes.telegram import MemorySecretBackend
from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
PATH = "/api/v1/settings/telegram"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _auth(app: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}  # type: ignore[attr-defined]


def _cli_json(output: str) -> dict[str, object]:
    return json.loads(output[output.find("{") :])


def test_cli_write_is_readable_through_the_api_and_vice_versa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    monkeypatch.delenv("OFT_TELEGRAM_BOT_TOKEN", raising=False)
    settings = _settings(tmp_path)
    # The CLI's ``load_settings`` must resolve the same data directory.
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))

    app = create_app(settings, clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        # Seed the write-only token through the API; both surfaces report it.
        token = client.put(
            f"{PATH}/token",
            json={"bot_token": SENTINEL},
            headers={**auth, "Idempotency-Key": "parity-token"},
        )
        assert token.status_code == 200, token.text

        # The CLI must resolve the same secret store and see the token the API
        # just wrote. The JSON status exposes ``token_configured``; the masked
        # value is rendered by the human status line.
        status_json = CliRunner().invoke(cli_app, ["telegram", "status", "--json"])
        assert status_json.exit_code == 0, status_json.output
        status_data = _cli_json(status_json.stdout)["data"]
        assert status_data["token_configured"] is True
        status_text = CliRunner().invoke(cli_app, ["telegram", "status"])
        assert status_text.exit_code == 0, status_text.output
        assert "****stuv" in status_text.stdout

        # (a) CLI write -> API read.
        written = CliRunner().invoke(
            cli_app,
            [
                "telegram",
                "toggles",
                "set",
                "--no-budget",
                "--chat-id",
                "1001",
                "--user-id",
                "2002",
                "--json",
            ],
        )
        assert written.exit_code == 0, written.output
        read_back = client.get(PATH, headers=auth)
        assert read_back.status_code == 200, read_back.text
        data = read_back.json()["data"]
        assert data["chat_ids"] == [1001]
        assert data["allowed_user_ids"] == [2002]
        assert data["toggles"]["budget"] is False
        assert data["toggles"]["risk"] is True
        assert data["token"]["configured"] is True

        # (b) API write -> CLI read.
        saved = client.put(
            PATH,
            json={"toggles": {"trigger": False}, "chat_ids": [3003]},
            headers={**auth, "Idempotency-Key": "parity-settings"},
        )
        assert saved.status_code == 200, saved.text

        shown = CliRunner().invoke(cli_app, ["telegram", "toggles", "show", "--json"])
        assert shown.exit_code == 0, shown.output
        payload = _cli_json(shown.stdout)["data"]
        assert payload["chat_ids"] == [3003]
        assert payload["allowed_user_ids"] == [2002]
        assert payload["toggles"]["trigger"] is False
        assert payload["toggles"]["budget"] is False
        assert payload["toggles"]["risk"] is True
        assert payload["source"] == "user"
