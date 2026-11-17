"""Deterministic, network-free Telegram port for ``OFT_ENV=test`` and tests.

The fake records every send attempt and returns scripted updates. It never
opens a socket and never needs a token, so the composition root can select it
without touching a real Bot API endpoint.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from oft.domain.telegram import TelegramUpdate, parse_update
from oft.ports.telegram import (
    TELEGRAM_UPSTREAM_ERROR,
    SentMessage,
    TelegramError,
    TelegramIdentity,
    TelegramUpdatePage,
)


def _as_update(item: object) -> TelegramUpdate | None:
    """Accept a normalized update or a raw Bot API update dict."""
    if isinstance(item, TelegramUpdate):
        return item
    return parse_update(item)


def _raw_update_id(item: object) -> int | None:
    """The positive integer ``update_id`` of a raw item, else ``None``."""
    if isinstance(item, TelegramUpdate):
        return item.update_id
    if not isinstance(item, Mapping):
        return None
    update_id = item.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id <= 0:
        return None
    return update_id


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
        self._raw_updates: list[object] = list(updates)
        self._next_message_id = sent_message_id_start
        self._failures_remaining = max(0, fail_send_times)
        self._failure_code = failure_code
        self.gate = gate
        self.entered = threading.Event()
        self.update_calls: list[int | None] = []
        self.send_calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def script_updates(self, updates: Iterable[object]) -> None:
        self._raw_updates = list(updates)

    def get_me(self) -> TelegramIdentity:
        return TelegramIdentity(user_id=42, username="oft_bot")

    def get_updates(
        self, *, offset: int | None = None, timeout_seconds: int = 0
    ) -> TelegramUpdatePage:
        self.update_calls.append(offset)
        updates: list[TelegramUpdate] = []
        max_update_id: int | None = None
        # Raw items are retained so a non-text update still contributes its
        # update id even though it never normalizes into a ``TelegramUpdate``.
        for raw in self._raw_updates:
            update_id = _raw_update_id(raw)
            if offset is not None and (update_id is None or update_id < offset):
                continue
            if update_id is not None:
                max_update_id = (
                    update_id if max_update_id is None else max(max_update_id, update_id)
                )
            parsed = _as_update(raw)
            if parsed is not None:
                updates.append(parsed)
        return TelegramUpdatePage(updates=tuple(updates), max_update_id=max_update_id)

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
