"""Telegram port: the only shape the application may use to reach the Bot API.

The port never exposes ``httpx``, a URL, or the token. Typed stable codes let
the API boundary map a transport failure to a catalog error without ever
shipping a raw Bot API body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from oft.domain.telegram import TelegramUpdate

TELEGRAM_NOT_CONFIGURED = "TELEGRAM_NOT_CONFIGURED"
TELEGRAM_UNAUTHORIZED = "TELEGRAM_UNAUTHORIZED"
TELEGRAM_UPSTREAM_ERROR = "TELEGRAM_UPSTREAM_ERROR"
TELEGRAM_RATE_LIMITED = "TELEGRAM_RATE_LIMITED"
TELEGRAM_CONFLICT = "TELEGRAM_CONFLICT"
TELEGRAM_TOKEN_INVALID = "TELEGRAM_TOKEN_INVALID"

PORT_ERROR_CODES = frozenset(
    {
        TELEGRAM_NOT_CONFIGURED,
        TELEGRAM_UNAUTHORIZED,
        TELEGRAM_UPSTREAM_ERROR,
        TELEGRAM_RATE_LIMITED,
        TELEGRAM_CONFLICT,
        TELEGRAM_TOKEN_INVALID,
    }
)


class TelegramError(Exception):
    """A typed Telegram failure with a stable, non-secret code.

    ``message`` is bounded and deliberately never contains the token, the
    request URL, or a raw Bot API response body. ``retry_after`` carries the Bot
    API ``parameters.retry_after`` for a rate limit, sanitized to a non-negative
    int (or ``None``).
    """

    def __init__(self, code: str, message: str, *, retry_after: int | None = None) -> None:
        if code not in PORT_ERROR_CODES:
            raise ValueError(f"unknown Telegram error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 0:
            retry_after = None
        self.retry_after = retry_after


@dataclass(frozen=True)
class TelegramIdentity:
    """The result of ``getMe``; only non-secret fields."""

    user_id: int
    username: str | None = None


@dataclass(frozen=True)
class TelegramUpdatePage:
    """One ``getUpdates`` page: parseable updates plus the raw max update id.

    ``max_update_id`` covers every raw item with a positive integer
    ``update_id``, including unparseable/non-text items. The poller uses it to
    acknowledge dropped updates so Telegram stops replaying them.
    """

    updates: tuple[TelegramUpdate, ...]
    max_update_id: int | None = None


@dataclass(frozen=True)
class SentMessage:
    """The Bot API's acceptance receipt for one message."""

    message_id: int


class TelegramPort(Protocol):
    """The three read/send operations the walking skeleton needs."""

    def get_me(self) -> TelegramIdentity: ...

    def get_updates(
        self, *, offset: int | None = None, timeout_seconds: int = 0
    ) -> TelegramUpdatePage: ...

    def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> SentMessage: ...


__all__ = [
    "PORT_ERROR_CODES",
    "TELEGRAM_CONFLICT",
    "TELEGRAM_NOT_CONFIGURED",
    "TELEGRAM_RATE_LIMITED",
    "TELEGRAM_TOKEN_INVALID",
    "TELEGRAM_UNAUTHORIZED",
    "TELEGRAM_UPSTREAM_ERROR",
    "SentMessage",
    "TelegramError",
    "TelegramIdentity",
    "TelegramPort",
    "TelegramUpdatePage",
]
