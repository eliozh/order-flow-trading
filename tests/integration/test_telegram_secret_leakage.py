"""Security regression: no Telegram or LLM secret reaches an observable surface.

The token and one provider key are written through the real encrypted-file
fallback (keyring backend forced to ``fail`` plus a per-test ``OFT_MASTER_KEY``),
then the real app is driven: token/allowlist through the API, one test send,
one dispatch, one query reply, one unauthorized update, and the CLI read
surfaces. Every audited surface is scanned for four sentinels: the bot token,
the LLM key, a raw-prompt sentinel, and a provider-error-body sentinel.

The DOM side of the same invariant is asserted by
``web/e2e/settings-telegram.spec.ts``: after save and after reload the token is
absent from ``page.content()`` and from session/local storage.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.api.app import create_app
from oft.application.secrets_service import SecretsService
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_poller import TelegramPoller
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.telegram import TelegramError

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
BOT_TOKEN = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
LLM_KEY = "sk-live-LLM-SENTINEL-DO-NOT-LEAK-7f3a"
RAW_PROMPT = "RAW-PROMPT-SENTINEL-DO-NOT-LEAK-2b7c"
ERROR_BODY = "PROVIDER-ERROR-BODY-SENTINEL-DO-NOT-LEAK-9d4e"
SENTINELS = {
    "bot token": BOT_TOKEN,
    "llm key": LLM_KEY,
    "raw prompt": RAW_PROMPT,
    "provider error body": ERROR_BODY,
}
TOKEN_PREFIX = BOT_TOKEN[:12]


@pytest.fixture(autouse=True)
def _hermetic_secret_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the encrypted-file store so every test is hermetic in CI."""
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("OFT_MASTER_KEY", "per-test-master-key-0123456789abcdef")
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if any surface opens a socket instead of using the fake port."""

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the secret-leak scan must never open a socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def _assert_clean(surface: str, data: str | bytes) -> None:
    text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else data
    leaked = [name for name, sentinel in SENTINELS.items() if sentinel in text]
    del text
    if leaked:
        pytest.fail("secret material reached " + surface + ": " + ", ".join(leaked))


def _db_surface(settings: Settings) -> str:
    database = settings.data_dir / "oft.db"
    parts = [database.read_bytes().decode("utf-8", errors="ignore")]
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                parts.append(repr(row))
    return "\n".join(parts)


def _db_file_bytes(data_dir: Path) -> bytes:
    """The main database plus any live WAL/SHM sidecars, so a writer is covered."""
    return b"\n".join(path.read_bytes() for path in sorted(data_dir.glob("oft.db*")))


def _api_surfaces(client: TestClient, auth: dict[str, str]) -> list[tuple[str, str]]:
    responses = [
        ("/api/v1/telegram/status", client.get("/api/v1/telegram/status", headers=auth)),
        ("/api/v1/telegram/receipts", client.get("/api/v1/telegram/receipts", headers=auth)),
        ("/api/v1/settings/telegram", client.get("/api/v1/settings/telegram", headers=auth)),
        ("/api/v1/status", client.get("/api/v1/status", headers=auth)),
    ]
    surfaces: list[tuple[str, str]] = []
    for name, response in responses:
        # Every bearer-authenticated read must stay authorized: a 401 would
        # replace the scanned body with the auth-error page and make the scan
        # vacuous.
        assert response.status_code == 200, (name, response.status_code)
        surfaces.append((name, response.text))
    # The unauthenticated root may legitimately be served or refused.
    root = client.get("/")
    assert root.status_code in (200, 401), ("/", root.status_code)
    surfaces.append(("/", root.text))
    return surfaces


def _cli_surfaces(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    surfaces: list[tuple[str, str]] = []
    for name, args in (
        ("cli status", ["telegram", "status"]),
        ("cli receipts", ["telegram", "receipts"]),
        ("cli allowlist", ["telegram", "allowlist", "show"]),
        ("cli toggles", ["telegram", "toggles", "show"]),
        ("cli test", ["telegram", "test"]),
    ):
        result = CliRunner().invoke(cli_app, args)
        assert result.exit_code == 0, (name, result.output)
        surfaces.append((name + " stdout", result.stdout))
        stderr = getattr(result, "stderr", "")
        surfaces.append((name + " stderr", stderr if isinstance(stderr, str) else str(stderr)))
    return surfaces


def test_no_sentinel_reaches_db_api_cli_logs_audit_receipts_or_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    container = app.state.container
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    client = TestClient(app)

    # A provider key goes through the same real encrypted-file backend.
    llm_status = SecretsService(
        store=container.store, settings=settings, clock=FixedClock(NOW)
    ).set_key("openai", LLM_KEY, actor="test")
    assert llm_status.key_source == "encrypted_file"

    try:
        # Configure and drive the real API path. The mutating responses are
        # scanned too: a token or key echoed back by a PUT/POST must not pass.
        api_surfaces: list[tuple[str, str]] = []
        token = client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": BOT_TOKEN},
            headers={**auth, "Idempotency-Key": "leak-token"},
        )
        assert token.status_code == 200, token.text
        _assert_clean("token PUT response", token.text)
        api_surfaces.append(("token PUT response", token.text))
        allowlist = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**auth, "Idempotency-Key": "leak-allowlist"},
        )
        assert allowlist.status_code == 200, allowlist.text
        _assert_clean("allowlist PUT response", allowlist.text)
        api_surfaces.append(("allowlist PUT response", allowlist.text))
        test_send = client.post(
            "/api/v1/telegram/test",
            json={},
            headers={**auth, "Idempotency-Key": "leak-test"},
        )
        assert test_send.status_code == 200, test_send.text
        _assert_clean("telegram test POST response", test_send.text)
        api_surfaces.append(("telegram test POST response", test_send.text))

        dispatcher = OutboxDispatcher(
            store=container.store,
            clock=container.clock,
            port_resolver=lambda: container.telegram_port,
            jitter=lambda: 0.0,
        )
        assert dispatcher.dispatch_once() == 1

        # A valid query reply plus an unauthorized update carrying the raw
        # prompt and the provider-error sentinel: neither may be echoed.
        container.telegram_port.script_updates(
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 101,
                        "chat": {"id": 1001, "type": "private"},
                        "from": {"id": 2002},
                        "text": "/positions",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 102,
                        "chat": {"id": 9999, "type": "private"},
                        "from": {"id": 9999},
                        "text": f"/approve {RAW_PROMPT} {ERROR_BODY}",
                    },
                },
            ]
        )
        poller = TelegramPoller(
            service=container.telegram_service,
            store=container.store,
            clock=container.clock,
            port_resolver=lambda: container.telegram_port,
            long_poll_timeout=0,
            poll_interval_seconds=0.01,
        )
        assert poller.poll_once() == 2
        dispatcher.dispatch_once()

        # A provider/error body in a Bot API error response is never surfaced.
        class _HostileBody(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    500,
                    json={
                        "ok": False,
                        "error_code": 500,
                        "description": f"upstream said {ERROR_BODY}",
                    },
                )

        hostile = TelegramBotApiAdapter(token_resolver=lambda: BOT_TOKEN, transport=_HostileBody())
        with pytest.raises(TelegramError) as caught:
            hostile.send_message(chat_id=1001, text="STATUS")
        _assert_clean("adapter error message", str(caught.value))

        cli_surfaces = _cli_surfaces(settings.data_dir, monkeypatch)
        api_surfaces.extend(_api_surfaces(client, auth))
    finally:
        client.close()

    # Prove the sentinel is genuinely stored and decryptable before scanning:
    # a silently failed token write would otherwise make the scan vacuous.
    secret_meta = container.store.get_secret_meta("telegram", "bot_token")
    assert secret_meta is not None
    assert secret_meta.backend == "encrypted_file"
    assert (
        SecretsService(
            store=container.store, settings=settings, clock=FixedClock(NOW)
        ).resolve_secret("telegram", "bot_token")
        == BOT_TOKEN
    )

    log_text = (settings.data_dir / "logs" / "daemon.log").read_text(encoding="utf-8")
    assert "secret_stored" in log_text or "browser_bootstrap_ready" in log_text
    audit_rows = "\n".join(
        event.payload_json for event in container.store.list_audit_events_ascending()
    )
    receipts = container.store.list_telegram_receipts(limit=100)
    receipt_rows = "\n".join(record.payload_json for record in receipts)
    outbound = "\n".join(message["text"] for message in container.telegram_port.sent)
    secret_meta_text = json.dumps(secret_meta.__dict__ if secret_meta is not None else {})

    surfaces: list[tuple[str, str | bytes]] = [
        ("raw sqlite bytes", _db_file_bytes(settings.data_dir)),
        ("sqlite tables", _db_surface(settings)),
        ("api responses", "\n".join(text for _name, text in api_surfaces)),
        ("cli output", "\n".join(text for _name, text in cli_surfaces)),
        ("daemon log", log_text),
        ("audit events", audit_rows),
        ("receipt payloads", receipt_rows),
        ("outbound messages", outbound),
        ("secret metadata", secret_meta_text),
        ("encrypted secret file", (settings.data_dir / "secrets.enc").read_bytes()),
    ]
    for name, payload in surfaces:
        _assert_clean(name, payload)

    # The mask is the only token-shaped value any surface may expose.
    assert TOKEN_PREFIX not in log_text
    assert BOT_TOKEN not in json.dumps(
        [message["text"] for message in container.telegram_port.sent]
    )
    assert receipts
    assert any(record.kind == "test" for record in receipts)
