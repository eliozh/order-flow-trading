"""Deterministic, network-free Telegram port for ``OFT_ENV=test`` and tests.

The fake records every send attempt and returns scripted updates. It never
opens a socket and never needs a token, so the composition root can select it
without touching a real Bot API endpoint.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from oft.domain.telegram import TelegramUpdate, parse_update
from oft.ports.telegram import (
    TELEGRAM_UPSTREAM_ERROR,
    SentMessage,
    TelegramError,
    TelegramIdentity,
)


def _as_update(item: object) -> TelegramUpdate | None:
    """Accept a normalized update or a raw Bot API update dict."""
    if isinstance(item, TelegramUpdate):
        return item
    return parse_update(item)


class TelegramFake:
    """A scripted :class:`oft.ports.telegram.TelegramPort` implementation."""

    def __init__(
        self,
        *,
        updates: Iterable[object] = (),
        sent_message_id_start: int = 500,
        fail_send_times: int = 0,
        failure_code: str = TELEGRAM_UPSTREAM_ERROR,
        gate: threading.Event | None = None,
    ) -> None:
        self._updates: list[TelegramUpdate] = [
            parsed for parsed in map(_as_update, updates) if parsed is not None
        ]
        self._next_message_id = sent_message_id_start
        self._failures_remaining = max(0, fail_send_times)
        self._failure_code = failure_code
        self.gate = gate
        self.entered = threading.Event()
        self.update_calls: list[int | None] = []
        self.send_calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def script_updates(self, updates: Iterable[object]) -> None:
        self._updates = [parsed for parsed in map(_as_update, updates) if parsed is not None]

    def get_me(self) -> TelegramIdentity:
        return TelegramIdentity(user_id=42, username="oft_bot")

    def get_updates(
        self, *, offset: int | None = None, timeout_seconds: int = 0
    ) -> tuple[TelegramUpdate, ...]:
        self.update_calls.append(offset)
        return tuple(
            update for update in self._updates if offset is None or update.update_id >= offset
        )

    def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> SentMessage:
        call = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
        }
        self.send_calls.append(call)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise TelegramError(self._failure_code, "scripted Telegram delivery failure")
        self._next_message_id += 1
        message_id = self._next_message_id
        self.sent.append({**call, "message_id": message_id})
        return SentMessage(message_id=message_id)


__all__ = ["TelegramFake"]
