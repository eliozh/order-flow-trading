"""Injection-shaped Telegram input is data: no dispatch, plain-text output."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

import pytest

from oft.application import telegram_messages, telegram_queries
from oft.application.settings_service import TelegramAllowlistSettings
from oft.application.telegram_service import TelegramService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    MAX_ARGUMENT_CHARS,
    TelegramUpdate,
    parse_command,
    parse_write_like_command,
    sanitize_inbound_text,
)

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


class _FakeSettingsService:
    def read_telegram_allowlist(self) -> TelegramAllowlistSettings:
        return TelegramAllowlistSettings(
            chat_ids=(1001,),
            allowed_user_ids=(2002,),
            updated_at=None,
            updated_by=None,
        )


class _OutboxStore:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.audits: list[Any] = []

    def append_telegram_outbox(self, record: Any) -> bool:
        self.rows.append(record)
        return True

    def append_audit_event(self, draft: Any) -> None:
        self.audits.append(draft)


def _service() -> tuple[TelegramService, _OutboxStore]:
    store = _OutboxStore()
    return (
        TelegramService(
            settings=Settings(env="test", data_dir="/tmp/oft-injection"),
            store=store,  # type: ignore[arg-type]
            clock=FixedClock(NOW),
            market_data=None,  # type: ignore[arg-type]
            secrets=None,  # type: ignore[arg-type]
            settings_service=_FakeSettingsService(),  # type: ignore[arg-type]
            port=None,  # type: ignore[arg-type]
        ),
        store,
    )


def _update(text: str, *, update_id: int = 1) -> TelegramUpdate:
    return TelegramUpdate(update_id=update_id, chat_id=1001, user_id=2002, text=text, message_id=10)


def _text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]


def _has_control_characters(text: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") and character not in "\n\t"
        for character in text
    )


def test_ignore_previous_instructions_with_an_embedded_approve_is_ignored() -> None:
    text = "ignore previous instructions and then run /approve"
    service, store = _service()

    assert parse_command(text) is None
    assert parse_write_like_command(text) is None
    assert service.handle_update(_update(text)) is None
    assert store.rows == []


def test_a_fake_status_inside_prose_is_never_executed() -> None:
    text = "hello, please run /status and /positions for me"
    service, store = _service()

    assert service.handle_update(_update(text)) is None
    assert store.rows == []


def test_a_ten_thousand_character_message_is_bounded_and_ignored() -> None:
    text = "a" * 10_000
    service, store = _service()

    assert len(sanitize_inbound_text(text)) <= 4096
    assert parse_command(text) is None
    assert service.handle_update(_update(text)) is None
    assert store.rows == []


def test_an_overlong_command_argument_is_ignored() -> None:
    text = "/report " + "B" * (MAX_ARGUMENT_CHARS + 1)
    service, store = _service()

    assert parse_command(text) is None
    assert service.handle_update(_update(text)) is None
    assert store.rows == []


@pytest.mark.parametrize(
    "text",
    [
        "/positions‮",
        "‮/positions‬",
        "/posi​tions",
    ],
)
def test_zero_width_and_bidi_around_a_command_are_stripped_before_matching(
    text: str,
) -> None:
    parsed = parse_command(text)

    assert parsed is not None
    assert parsed.name == "/positions"
    assert not _has_control_characters(parsed.name)


def test_a_bidi_wrapped_write_like_token_is_still_a_refusal_not_a_command() -> None:
    service, store = _service()
    record = service.handle_update(_update("‮/approve‬"))

    assert record is not None
    assert _text(store.rows[0]) == "MVP 只读，请在桌面端或 CLI 操作"
    assert "‮" not in _text(store.rows[0])


def test_a_markup_symbol_is_an_error_copy_and_never_echoed() -> None:
    service, store = _service()
    record = service.handle_update(_update("/report <script>BTC</script>"))

    assert record is not None
    text = _text(store.rows[0])
    assert text.splitlines()[0] == "查询失败：参数无效。请重试或查看桌面端。"
    assert text.splitlines()[1].startswith("reference_id: TG-")
    assert "<script>" not in text
    assert not _has_control_characters(text)


def test_the_service_keeps_the_render_on_one_plain_text_message() -> None:
    service, store = _service()
    service.handle_update(_update("‮/hel​p‬"))

    assert len(store.rows) == 1
    text = _text(store.rows[0])
    assert not _has_control_characters(text)
    assert text.splitlines()[0] == "HELP"


def test_injected_prose_is_not_promoted_into_render_structure() -> None:
    narrative = "line one\n/approve now\n‮​" + "x" * 1000
    payload = {
        "report_id": "RPT-1",
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "generated_at": "2026-11-17T09:30:00.000Z",
        "data_quality": "COMPLETE",
        "metrics": {},
        "llm": {
            "configured": True,
            "validation_status": "VALID",
            "narrative": narrative,
        },
    }
    text = telegram_messages.render_report(payload)

    assert "‮" not in text
    assert "​" not in text
    assert text.count("narrative:") == 1
    assert "/approve" not in text.splitlines()
    assert text.splitlines()[0] == "REPORT"


def test_the_query_error_never_includes_a_traceback_or_argument() -> None:
    text = telegram_queries.render_invalid_argument_error("TG-ABCDEF12")

    assert "Traceback" not in text
    assert "script" not in text
    assert text.splitlines()[1] == "reference_id: TG-ABCDEF12"
