"""Telegram adapter contract: map Bot API failures and never leak the token."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from fakes.telegram import BOT_API_HOST, MemorySecretBackend, ScriptedBotApiTransport
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.api.app import create_app
from oft.application import secrets_service as secrets_module
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.secrets import SecretStoreUnavailable
from oft.ports.telegram import TelegramError

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def envelope_validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    envelope = _load("api-envelope.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(envelope, registry=registry)


def _adapter(transport: ScriptedBotApiTransport) -> TelegramBotApiAdapter:
    return TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)


def test_get_me_uses_the_fixed_bot_api_host() -> None:
    transport = ScriptedBotApiTransport()

    identity = _adapter(transport).get_me()

    assert identity.username == "oft_bot"
    request = transport.requests[0]
    assert request.path == f"/bot{SENTINEL}/getMe"
    assert request.host == BOT_API_HOST.removeprefix("https://")
    assert SENTINEL not in json.dumps({"ok": True, "user": identity.username})


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (401, "TELEGRAM_TOKEN_INVALID"),
        (429, "TELEGRAM_RATE_LIMITED"),
        (409, "TELEGRAM_CONFLICT"),
        (400, "TELEGRAM_UPSTREAM_ERROR"),
        (500, "TELEGRAM_UPSTREAM_ERROR"),
    ],
)
def test_bot_api_errors_map_to_stable_codes(error_code: int, expected: str) -> None:
    transport = ScriptedBotApiTransport()
    transport.script_error("sendMessage", error_code)

    with pytest.raises(TelegramError) as caught:
        _adapter(transport).send_message(chat_id=1001, text="STATUS")

    assert caught.value.code == expected
    # The token and the raw Telegram body/description never appear in the error.
    assert SENTINEL not in str(caught.value)
    assert "scripted failure" not in str(caught.value)


def test_a_missing_token_is_not_configured_and_makes_no_request() -> None:
    transport = ScriptedBotApiTransport()
    adapter = TelegramBotApiAdapter(token_resolver=lambda: None, transport=transport)

    with pytest.raises(TelegramError) as caught:
        adapter.get_me()

    assert caught.value.code == "TELEGRAM_NOT_CONFIGURED"
    assert transport.requests == []


def test_an_invalid_shaped_env_token_is_token_invalid_and_makes_no_request() -> None:
    transport = ScriptedBotApiTransport()
    adapter = TelegramBotApiAdapter(token_resolver=lambda: "not-a-bot-token", transport=transport)

    with pytest.raises(TelegramError) as caught:
        adapter.get_me()

    assert caught.value.code == "TELEGRAM_TOKEN_INVALID"
    assert transport.requests == []
    assert "not-a-bot-token" not in str(caught.value)


def test_an_unavailable_secret_store_is_not_configured_and_makes_no_request() -> None:
    transport = ScriptedBotApiTransport()

    def _resolver() -> str:
        raise SecretStoreUnavailable(
            "the secure secret store is unavailable", reason="backend_unavailable"
        )

    adapter = TelegramBotApiAdapter(token_resolver=_resolver, transport=transport)

    with pytest.raises(TelegramError) as caught:
        adapter.get_me()

    assert caught.value.code == "TELEGRAM_NOT_CONFIGURED"
    assert transport.requests == []
    assert "backend_unavailable" not in str(caught.value)


def test_an_unexpected_resolver_failure_is_not_misclassified() -> None:
    transport = ScriptedBotApiTransport()

    def _resolver() -> str:
        raise RuntimeError("a programming error must propagate")

    adapter = TelegramBotApiAdapter(token_resolver=_resolver, transport=transport)

    with pytest.raises(RuntimeError):
        adapter.get_me()

    assert transport.requests == []


def test_a_transport_failure_is_a_bounded_upstream_error() -> None:
    class _BrokenTransport(ScriptedBotApiTransport):
        def handle_request(self, request: Any) -> Any:
            raise httpx.ConnectError(f"cannot reach {request.url}")

    adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=_BrokenTransport())

    with pytest.raises(TelegramError) as caught:
        adapter.get_me()

    assert caught.value.code == "TELEGRAM_UPSTREAM_ERROR"
    assert SENTINEL not in str(caught.value)


def test_send_message_never_sets_parse_mode_and_omits_reply_when_absent() -> None:
    transport = ScriptedBotApiTransport()

    sent = _adapter(transport).send_message(chat_id=1001, text="STATUS")

    assert sent.message_id == 501
    body = transport.requests[0].json
    assert body == {"chat_id": 1001, "text": "STATUS"}
    assert "parse_mode" not in body


def test_send_message_carries_the_reply_message_id() -> None:
    transport = ScriptedBotApiTransport()

    _adapter(transport).send_message(chat_id=1001, text="STATUS", reply_to_message_id=55)

    assert transport.requests[0].json["reply_to_message_id"] == 55


def test_the_telegram_error_codes_are_in_the_shared_catalog() -> None:
    from oft.api.errors import ERROR_CATALOG

    for code in ("TELEGRAM_NOT_CONFIGURED", "TELEGRAM_DELIVERY_FAILED", "TELEGRAM_TOKEN_INVALID"):
        assert code in ERROR_CATALOG


def test_the_api_responses_validate_against_the_envelope_schema(
    tmp_path: Any,
    envelope_validator: Draft202012Validator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        responses = [
            client.get("/api/v1/telegram/status", headers=auth),
            client.get("/api/v1/telegram/receipts", headers=auth),
            client.put(
                "/api/v1/settings/telegram/token",
                json={"bot_token": SENTINEL},
                headers={**auth, "Idempotency-Key": "c-token"},
            ),
            client.put(
                "/api/v1/settings/telegram/allowlist",
                json={"chat_ids": [1001], "allowed_user_ids": []},
                headers={**auth, "Idempotency-Key": "c-allow"},
            ),
            client.post(
                "/api/v1/telegram/test",
                json={},
                headers={**auth, "Idempotency-Key": "c-test"},
            ),
        ]

    for response in responses:
        errors = sorted(envelope_validator.iter_errors(response.json()), key=lambda e: list(e.path))
        assert not errors, "\n".join(error.message for error in errors)
        assert SENTINEL not in response.text


class _FixedResponseTransport(httpx.BaseTransport):
    def __init__(self, response: Any) -> None:
        self._response = response

    def handle_request(self, request: Any) -> Any:
        return self._response


def test_get_updates_parses_valid_items_and_skips_malformed() -> None:
    from fakes.telegram import make_update

    transport = _FixedResponseTransport(
        httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    make_update(1, text="/status"),
                    {"update_id": 2},
                    "not-an-update",
                    {"update_id": 3, "message": {"text": "no chat/sender"}},
                ],
            },
        )
    )

    page = _adapter_transport(transport).get_updates(offset=1)

    assert [update.update_id for update in page.updates] == [1]
    # The dropped non-text/malformed items still acknowledge their raw ids, so
    # the poller can advance past them instead of re-fetching them forever.
    assert page.max_update_id == 3


def test_a_rate_limited_error_carries_the_retry_after_parameter() -> None:
    transport = ScriptedBotApiTransport()
    transport.script_error("sendMessage", 429, retry_after=30)

    with pytest.raises(TelegramError) as caught:
        _adapter(transport).send_message(chat_id=1001, text="STATUS")

    assert caught.value.code == "TELEGRAM_RATE_LIMITED"
    assert caught.value.retry_after == 30


def test_retry_after_is_sanitized_to_a_non_negative_non_bool_int() -> None:
    assert TelegramError("TELEGRAM_RATE_LIMITED", "x", retry_after=True).retry_after is None
    assert TelegramError("TELEGRAM_RATE_LIMITED", "x", retry_after=-5).retry_after is None
    assert TelegramError("TELEGRAM_RATE_LIMITED", "x", retry_after="30").retry_after is None
    assert TelegramError("TELEGRAM_RATE_LIMITED", "x", retry_after=30).retry_after == 30
    assert TelegramError("TELEGRAM_RATE_LIMITED", "x").retry_after is None


def _adapter_transport(transport: Any) -> TelegramBotApiAdapter:
    return TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)


def test_a_malformed_get_me_result_is_a_bounded_error() -> None:
    for payload in ({"ok": True, "result": ["not-a-dict"]}, {"ok": True, "result": {"no_id": 1}}):
        transport = _FixedResponseTransport(httpx.Response(200, json=payload))
        with pytest.raises(TelegramError) as caught:
            _adapter_transport(transport).get_me()
        assert caught.value.code == "TELEGRAM_UPSTREAM_ERROR"
        assert SENTINEL not in str(caught.value)


def test_a_malformed_send_result_is_a_bounded_error() -> None:
    for payload in (
        {"ok": True, "result": "nope"},
        {"ok": True, "result": {"no_message_id": True}},
    ):
        transport = _FixedResponseTransport(httpx.Response(200, json=payload))
        with pytest.raises(TelegramError) as caught:
            _adapter_transport(transport).send_message(chat_id=1, text="x")
        assert caught.value.code == "TELEGRAM_UPSTREAM_ERROR"


def test_a_non_json_or_non_object_response_is_a_bounded_error() -> None:
    import httpx

    for response in (
        httpx.Response(502, text="<html>gateway</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ):
        transport = _FixedResponseTransport(response)
        with pytest.raises(TelegramError) as caught:
            _adapter_transport(transport).get_me()
        assert caught.value.code == "TELEGRAM_UPSTREAM_ERROR"
        assert SENTINEL not in str(caught.value)


def test_a_malformed_get_updates_result_is_a_bounded_error() -> None:
    import httpx

    transport = _FixedResponseTransport(httpx.Response(200, json={"ok": True, "result": "nope"}))
    with pytest.raises(TelegramError) as caught:
        _adapter_transport(transport).get_updates()
    assert caught.value.code == "TELEGRAM_UPSTREAM_ERROR"
