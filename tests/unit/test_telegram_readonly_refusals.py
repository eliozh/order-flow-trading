"""Write-like Telegram commands are refused without any query or mutation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from oft.application import telegram_queries
from oft.application import telegram_service as service_module
from oft.application.settings_service import TelegramAllowlistSettings
from oft.application.telegram_service import TelegramService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    REFUSAL_TEXT,
    WRITE_LIKE_COMMANDS,
    WRITE_REFUSAL_TEXT,
    TelegramUpdate,
)

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


class _FakeSettingsService:
    def __init__(self, allowlist: TelegramAllowlistSettings) -> None:
        self._allowlist = allowlist

    def read_telegram_allowlist(self) -> TelegramAllowlistSettings:
        return self._allowlist


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
    settings_service = _FakeSettingsService(
        TelegramAllowlistSettings(
            chat_ids=(1001,),
            allowed_user_ids=(2002,),
            updated_at=None,
            updated_by=None,
        )
    )
    service = TelegramService(
        settings=Settings(env="test", data_dir="/tmp/oft-refusal"),
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        market_data=None,  # type: ignore[arg-type]
        secrets=None,  # type: ignore[arg-type]
        settings_service=settings_service,  # type: ignore[arg-type]
        port=None,  # type: ignore[arg-type]
    )
    return service, store


def _update(text: str, *, update_id: int = 1, chat_id: int = 1001, user_id: int = 2002):
    return TelegramUpdate(
        update_id=update_id, chat_id=chat_id, user_id=user_id, text=text, message_id=10
    )


@pytest.mark.parametrize("token", WRITE_LIKE_COMMANDS)
def test_every_write_like_token_returns_only_the_fixed_refusal(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        calls.append("query")
        raise AssertionError("a write-like command must never run a query")

    for name in (
        "render_positions_query",
        "render_opportunities_query",
        "render_report_query",
        "render_budget_query",
        "render_watchlist_query",
    ):
        monkeypatch.setattr(telegram_queries, name, _boom)
    monkeypatch.setattr(service_module, "build_status", _boom)

    service, store = _service()
    record = service.handle_update(_update(token))

    assert record is not None
    assert calls == []
    assert len(store.rows) == 1
    assert _text(store.rows[0]) == WRITE_REFUSAL_TEXT


def test_a_write_like_token_with_arguments_is_still_refused() -> None:
    service, store = _service()
    service.handle_update(_update("/approve BTCUSDT"))

    assert _text(store.rows[0]) == WRITE_REFUSAL_TEXT


def test_an_unauthorized_sender_sees_unauthorized_not_the_write_refusal() -> None:
    service, store = _service()
    service.handle_update(_update("/approve", chat_id=9999))

    assert _text(store.rows[0]) == REFUSAL_TEXT


def test_a_write_like_token_inside_prose_is_ignored() -> None:
    service, store = _service()
    assert service.handle_update(_update("please /approve this")) is None
    assert store.rows == []


def _text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]
