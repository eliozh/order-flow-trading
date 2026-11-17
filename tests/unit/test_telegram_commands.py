"""Telegram command grammar, sanitization, authorization, and token rules."""

from __future__ import annotations

import pytest

from oft.domain.settings import SettingValidationError
from oft.domain.telegram import (
    HELP_COMMAND,
    MAX_ARGUMENT_CHARS,
    MAX_OUTBOUND_CHARS,
    REFUSAL_TEXT,
    STATUS_COMMAND,
    TelegramAllowlist,
    TelegramValidationError,
    is_authorized,
    parse_command,
    parse_update,
    reply_event_key,
    sanitize_argument,
    sanitize_inbound_text,
    truncate_outbound,
    validate_telegram_token,
)
from oft.domain.telegram import (
    test_event_key as make_test_event_key,
)

SENTINEL_TOKEN = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


class TestCommandGrammar:
    def test_status_and_help_are_the_only_commands(self) -> None:
        assert parse_command("/status") is not None
        assert parse_command("/status").name == STATUS_COMMAND  # type: ignore[union-attr]
        assert parse_command("/help") is not None
        assert parse_command("/help").name == HELP_COMMAND  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "text",
        ["/approve", "/statusx", "status", "//status", "/Status", "/STATUS"],
    )
    def test_unknown_or_inexact_commands_are_not_parsed(self, text: str) -> None:
        assert parse_command(text) is None

    @pytest.mark.parametrize(
        "text",
        ["please run /status", "hello", "  ", "", None],
    )
    def test_prose_or_empty_text_never_becomes_a_command(self, text: str | None) -> None:
        assert parse_command(text) is None  # type: ignore[arg-type]

    def test_control_and_zero_width_characters_are_stripped_before_matching(self) -> None:
        assert sanitize_inbound_text("/sta\x00tus") == "/status"
        assert sanitize_inbound_text("/sta​tus") == "/status"
        assert sanitize_inbound_text("/sta‮tus") == "/status"
        assert sanitize_inbound_text("/status\u009f") == "/status"
        assert parse_command("/status\x00").name == STATUS_COMMAND  # type: ignore[union-attr]

    def test_whitespace_is_collapsed(self) -> None:
        assert sanitize_inbound_text("/status   \t\n") == "/status"
        assert parse_command("/status").argument == ""  # type: ignore[union-attr]

    def test_argument_is_capped_and_overlong_commands_are_ignored(self) -> None:
        assert len(sanitize_argument("a" * 900)) == MAX_ARGUMENT_CHARS
        assert parse_command("/status " + "a" * (MAX_ARGUMENT_CHARS + 1)) is None
        parsed = parse_command("/status " + "a" * 10)
        assert parsed is not None and parsed.argument == "a" * 10

    def test_outbound_text_is_capped_at_the_message_limit(self) -> None:
        assert len(truncate_outbound("x" * (MAX_OUTBOUND_CHARS + 100))) == MAX_OUTBOUND_CHARS
        assert truncate_outbound("12345") == "12345"

    def test_event_keys_are_deterministic(self) -> None:
        assert reply_event_key(7) == "update:7:reply"
        assert make_test_event_key(11, "abc") == "test:11:abc"

    def test_refusal_text_is_the_fixed_exact_value(self) -> None:
        assert REFUSAL_TEXT == "未授权"


class TestUpdateParsing:
    def _raw(self, chat_id: object, user_id: object = 2002) -> dict[str, object]:
        return {
            "update_id": 1,
            "message": {
                "message_id": 101,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id},
                "text": "/status",
            },
        }

    def test_a_negative_group_chat_id_is_parsed(self) -> None:
        update = parse_update(self._raw(-1001234567890))
        assert update is not None
        assert update.chat_id == -1001234567890
        assert update.user_id == 2002

    @pytest.mark.parametrize("chat_id", [0, True, "10", 10.5, None])
    def test_a_non_integer_or_zero_chat_id_is_skipped(self, chat_id: object) -> None:
        assert parse_update(self._raw(chat_id)) is None

    @pytest.mark.parametrize("user_id", [0, -5, True, "20", 20.5, None])
    def test_a_non_positive_user_id_is_skipped(self, user_id: object) -> None:
        assert parse_update(self._raw(10, user_id)) is None


class TestAuthorization:
    def test_allowlisted_chat_and_user_is_authorized(self) -> None:
        allowlist = TelegramAllowlist(chat_ids=(10,), allowed_user_ids=(20,))
        assert is_authorized(10, 20, allowlist) is True

    def test_unknown_chat_is_refused(self) -> None:
        allowlist = TelegramAllowlist(chat_ids=(10,), allowed_user_ids=(20,))
        assert is_authorized(11, 20, allowlist) is False

    def test_unknown_user_is_refused_when_a_user_allowlist_exists(self) -> None:
        allowlist = TelegramAllowlist(chat_ids=(10,), allowed_user_ids=(20,))
        assert is_authorized(10, 21, allowlist) is False

    def test_an_empty_user_allowlist_refuses_every_user(self) -> None:
        # Fail-closed: a chat-only allowlist authorizes nobody.
        allowlist = TelegramAllowlist(chat_ids=(10,), allowed_user_ids=())
        assert is_authorized(10, 999, allowlist) is False

    def test_a_negative_group_chat_id_is_authorized_when_both_lists_match(self) -> None:
        allowlist = TelegramAllowlist(chat_ids=(-1001234567890,), allowed_user_ids=(20,))
        assert is_authorized(-1001234567890, 20, allowlist) is True

    def test_empty_chat_allowlist_refuses_everything(self) -> None:
        assert is_authorized(10, 20, TelegramAllowlist((), ())) is False


class TestTokenValidation:
    def test_a_bot_api_shaped_token_is_accepted(self) -> None:
        assert validate_telegram_token(SENTINEL_TOKEN) == SENTINEL_TOKEN
        assert validate_telegram_token(f"  {SENTINEL_TOKEN}  ") == SENTINEL_TOKEN

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-token",
            "123456:short",
            "abcdef789:AAFakeSentinelToken_abcdefghijklmnopqrstuv",
            "123456789:AAFake SentinelToken_abcdefghijklmnopqrstuv",
        ],
    )
    def test_invalid_tokens_are_rejected_without_echoing_the_value(self, value: str) -> None:
        with pytest.raises(TelegramValidationError) as caught:
            validate_telegram_token(value)
        if value:
            assert value not in str(caught.value)
        assert caught.value.details[0]["field"] == "bot_token"

    def test_non_string_is_rejected(self) -> None:
        with pytest.raises(TelegramValidationError):
            validate_telegram_token(123456789)  # type: ignore[arg-type]

    def test_the_domain_error_is_a_setting_validation_error(self) -> None:
        with pytest.raises(SettingValidationError):
            validate_telegram_token("bad")


def test_an_oversized_token_is_rejected_without_echoing_it() -> None:
    value = "123456789:" + "A" * 600
    with pytest.raises(TelegramValidationError) as caught:
        validate_telegram_token(value)
    assert value not in str(caught.value)
    assert caught.value.details[0]["code"] == "too_long"
