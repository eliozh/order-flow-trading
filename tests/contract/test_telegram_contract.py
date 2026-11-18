"""Telegram adapter contract: map Bot API failures and never leak the token."""

from __future__ import annotations

import json
import unicodedata
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


def test_the_public_telegram_codes_match_the_catalog_and_envelope() -> None:
    from oft.api.errors import ERROR_CATALOG

    catalog = {entry["code"]: entry for entry in _load("error-catalog.json")["codes"]}
    envelope_codes = set(_load("api-envelope.schema.json")["$defs"]["errorCode"]["enum"])
    for code in ("TELEGRAM_NOT_CONFIGURED", "TELEGRAM_DELIVERY_FAILED", "TELEGRAM_TOKEN_INVALID"):
        spec = ERROR_CATALOG[code]
        entry = catalog[code]
        assert code in envelope_codes
        assert (spec.http_status, spec.category, spec.retryable) == (
            entry["httpStatus"],
            entry["category"],
            entry["retryable"],
        )
    # Internal receipt/state codes are intentionally not public API errors.
    for internal in ("TELEGRAM_QUEUE_FULL", "TELEGRAM_LOCAL_RATE_LIMITED"):
        assert internal not in catalog
        assert internal not in envelope_codes


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
        # The added settings reads/writes are asserted as success responses too,
        # so an error envelope cannot pass schema validation unnoticed.
        get_settings = client.get("/api/v1/settings/telegram", headers=auth)
        assert get_settings.status_code == 200, get_settings.text
        put_settings = client.put(
            "/api/v1/settings/telegram",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**auth, "Idempotency-Key": "c-settings"},
        )
        assert put_settings.status_code == 200, put_settings.text

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
            get_settings,
            put_settings,
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


# -- slice 55: status/receipt/settings/message contracts --------------------


TELEGRAM_FIXTURES = ROOT / "tests" / "fixtures" / "telegram"


def _load_fixture(relative: str) -> Any:
    return json.loads((TELEGRAM_FIXTURES / relative).read_text(encoding="utf-8"))


def _fixture_report_payload() -> dict[str, Any]:
    """The renderer input built from the committed report read model."""
    report = _load_fixture("read_models/report.json")
    return {
        "report_id": report["report_id"],
        "symbol": report["symbol"],
        "timeframe": report["timeframe"],
        "generated_at": report["generated_at"],
        "data_quality": report["data_quality"],
        "metrics": report["metrics"],
        "snapshot": report["snapshot"],
        "llm": report["llm"],
    }


CONTROL_EXEMPT = frozenset({"\n", "\t"})
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, emoticons, transport, supplemental
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows
    (0xFE00, 0xFE0F),  # variation selectors
    (0x1F1E6, 0x1F1FF),  # regional-indicator flags
)
_ASCII_ART_RANGES = (
    (0x2500, 0x257F),  # box drawing
    (0x2580, 0x259F),  # block elements
)


def _has_control_characters(text: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") and character not in CONTROL_EXEMPT
        for character in text
    )


def _has_emoji(text: str) -> bool:
    for character in text:
        point = ord(character)
        if unicodedata.category(character) == "So":
            return True
        if any(low <= point <= high for low, high in _EMOJI_RANGES):
            return True
    return False


def _has_ascii_art(text: str) -> bool:
    return any(
        any(low <= ord(character) <= high for low, high in _ASCII_ART_RANGES) for character in text
    )


def test_the_committed_failure_shapes_map_to_their_stable_codes() -> None:
    fixtures = _load_fixture("failures/bot_api.json")["failures"]

    # The fixture must cover every required failure category, so deleting a
    # shape cannot silently narrow the contract.
    statuses = {fixture.get("http_status") for fixture in fixtures}
    assert {401, 409, 429} <= statuses
    assert any(status in {500, 502, 503, 504} for status in statuses)
    assert any(fixture.get("transport") == "timeout" for fixture in fixtures)
    assert {fixture["expected_code"] for fixture in fixtures} == {
        "TELEGRAM_TOKEN_INVALID",
        "TELEGRAM_CONFLICT",
        "TELEGRAM_RATE_LIMITED",
        "TELEGRAM_UPSTREAM_ERROR",
    }
    rate_limited = next(fixture for fixture in fixtures if fixture.get("http_status") == 429)
    assert rate_limited["retry_after"] == 30

    for fixture in fixtures:
        if fixture.get("transport") == "timeout":
            transport: Any = _TimeoutTransport()
        else:
            transport = _FixedResponseTransport(
                httpx.Response(fixture["http_status"], json=fixture["body"])
            )
        adapter = _adapter_transport(transport)
        with pytest.raises(TelegramError) as caught:
            if fixture["method"] == "getMe":
                adapter.get_me()
            elif fixture["method"] == "getUpdates":
                adapter.get_updates()
            else:
                adapter.send_message(chat_id=1001, text="STATUS")
        assert caught.value.code == fixture["expected_code"], fixture["name"]
        assert caught.value.retry_after == fixture["retry_after"], fixture["name"]


class _TimeoutTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


def test_status_receipt_and_settings_payloads_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _registry import validate

    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        token = client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers={**auth, "Idempotency-Key": "contract-telegram-token"},
        )
        assert token.status_code == 200, token.text
        allowlist = client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**auth, "Idempotency-Key": "contract-telegram-allowlist"},
        )
        assert allowlist.status_code == 200, allowlist.text
        test_send = client.post(
            "/api/v1/telegram/test",
            json={},
            headers={**auth, "Idempotency-Key": "contract-telegram-test"},
        )
        assert test_send.status_code == 200, test_send.text
        status = client.get("/api/v1/telegram/status", headers=auth)
        receipts = client.get("/api/v1/telegram/receipts", headers=auth)
        settings_response = client.get("/api/v1/settings/telegram", headers=auth)
        for response in (status, receipts, settings_response):
            assert response.status_code == 200, response.text
            assert SENTINEL not in response.text

    validate("telegram-status.schema.json", status.json()["data"])
    validate("telegram-settings.schema.json", settings_response.json()["data"])
    validate("telegram-receipt.schema.json", test_send.json()["data"])
    for receipt in receipts.json()["data"]["items"]:
        validate("telegram-receipt.schema.json", receipt)
    settings_payload = settings_response.json()["data"]
    assert settings_payload["latest_test_receipt"] is not None
    assert settings_payload["token"]["configured"] is True
    assert settings_payload["token"]["masked"] == "****stuv"
    assert SENTINEL not in json.dumps(settings_payload)


def test_receipt_last_error_code_accepts_the_internal_state_codes() -> None:
    from _registry import validate

    base = {
        "event_key": "update:1:reply",
        "kind": "query_reply",
        "chat_id": 1001,
        "reply_to_message_id": 101,
        "state": "queued",
        "attempts": 0,
        "message_id": None,
        "last_error_code": None,
        "next_attempt_at": NOW_TEXT,
        "created_at": NOW_TEXT,
        "sent_at": None,
    }
    for code in (
        "TELEGRAM_QUEUE_FULL",
        "TELEGRAM_LOCAL_RATE_LIMITED",
        "TELEGRAM_RATE_LIMITED",
        "TELEGRAM_UPSTREAM_ERROR",
        "TELEGRAM_NOT_CONFIGURED",
        "TELEGRAM_DELIVERY_FAILED",
        "TELEGRAM_TOKEN_INVALID",
        None,
    ):
        validate("telegram-receipt.schema.json", {**base, "last_error_code": code})


def test_message_shape_fixtures_validate_and_keep_the_first_line_plain() -> None:
    from _registry import validate

    from oft.application.telegram_messages import render_budget, render_help, render_report

    messages = _load_fixture("messages/shapes.json")["messages"]
    assert messages
    for message in messages:
        validate("telegram-message.schema.json", message)
        text = message["text"]
        assert message["first_line"] == text.splitlines()[0]
        # Recompute every declared plain-text property from the text itself;
        # a self-declared ``false`` must never mask a real emoji or box glyph.
        assert message["has_control_characters"] == _has_control_characters(text)
        assert message["has_emoji"] == _has_emoji(text)
        assert message["has_ascii_art"] == _has_ascii_art(text)
        assert not _has_control_characters(text)
        assert not _has_emoji(text)
        assert not _has_ascii_art(text)

    # Pin the hand-authored shapes to the production renderers so they cannot
    # drift again (HELP used to omit four commands; BUDGET omitted USAGE;
    # REPORT omitted the open-interest/funding/liquidation lines).
    by_event = {message["event"]: message for message in messages}
    assert by_event["HELP"]["text"] == render_help()
    assert by_event["REPORT"]["text"] == render_report(_fixture_report_payload())
    assert by_event["BUDGET"]["text"] == render_budget(
        {
            "usage_quality": "COMPLETE",
            "budget_state": "OK",
            "thresholds": {
                "daily_usd": "10",
                "weekly_usd": "50",
                "monthly_usd": "150",
                "warn_pct": "80",
            },
        }
    )


def test_existing_read_model_fixtures_still_validate() -> None:
    from _registry import validate

    validate(
        "position.schema.json",
        _load_fixture("read_models/position.json"),
    )
    validate(
        "opportunity.schema.json",
        _load_fixture("read_models/opportunity.json"),
    )
    validate(
        "budget-record.schema.json",
        _load_fixture("read_models/budget.json"),
    )
    # The existing provider-facing opportunity fixture still validates too.
    validate(
        "llm-output.schema.json",
        json.loads(
            (ROOT / "tests/fixtures/llm/opportunity_valid.json").read_text(encoding="utf-8")
        ),
    )

    from oft.application.telegram_messages import render_report

    rendered = render_report(_fixture_report_payload())
    assert rendered.splitlines()[0] == "REPORT"
    assert SENTINEL not in rendered


NOW_TEXT = "2026-11-17T09:30:00.000Z"
