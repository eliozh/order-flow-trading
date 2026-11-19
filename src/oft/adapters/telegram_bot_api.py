"""Telegram Bot API adapter: the one place the token is placed in a URL.

The egress is a fixed host (``https://api.telegram.org``) over an injected
``httpx.BaseTransport``. No webhook, no SDK, and no ``parse_mode``. The token
is resolved lazily on each call through the injected resolver, so a missing
token never blocks composition and never needs to be held by the adapter.

Security invariants:
- the token is never logged and never appears in an exception message;
- a Bot API error is mapped to a stable code using only its numeric
  ``error_code`` (and the HTTP status), never its ``description`` body;
- an unexpected transport failure becomes a bounded upstream error.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import structlog

from oft.domain.telegram import (
    TelegramUpdate,
    TelegramValidationError,
    parse_update,
    validate_telegram_token,
)
from oft.ports.secrets import SecretStoreUnavailable
from oft.ports.telegram import (
    TELEGRAM_CONFLICT,
    TELEGRAM_NOT_CONFIGURED,
    TELEGRAM_RATE_LIMITED,
    TELEGRAM_TOKEN_INVALID,
    TELEGRAM_UPSTREAM_ERROR,
    SentMessage,
    TelegramError,
    TelegramIdentity,
    TelegramUpdatePage,
)

LOGGER = structlog.get_logger(__name__)

BOT_API_HOST = "https://api.telegram.org"
# Must exceed the poller's 25 s long poll, otherwise every getUpdates would
# time out client-side before the Bot API answers.
DEFAULT_TIMEOUT_SECONDS = 35.0
# ``getUpdates`` long-poll timeout; the transport timeout must exceed it.
DEFAULT_LONG_POLL_SECONDS = 25


class TelegramBotApiAdapter:
    """Raw Bot API calls over the injected transport; never a live test call."""

    def __init__(
        self,
        *,
        token_resolver: Callable[[], str | None],
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token_resolver = token_resolver
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def _client_or_create(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                kwargs: dict[str, Any] = {
                    "base_url": BOT_API_HOST,
                    "timeout": self._timeout,
                    # The bot token is interpolated into the URL path. Ignore
                    # ambient proxy/.netrc/SSL_CERT_FILE settings so a
                    # TLS-inspecting proxy can never see it (SEC-01).
                    "trust_env": False,
                }
                if self._transport is not None:
                    kwargs["transport"] = self._transport
                self._client = httpx.Client(**kwargs)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _token(self) -> str:
        try:
            token = self._token_resolver()
        except (SecretStoreUnavailable, TelegramError) as exc:
            # An unavailable secret store (or an already-typed Telegram failure
            # from the resolver) is an operator-actionable configuration state.
            # Log only the bounded type/reason, never the value, then fail
            # closed. Any other exception is a programming error and propagates
            # rather than being misreported as "not configured".
            LOGGER.warning(
                "telegram_token_resolution_failed",
                error_type=type(exc).__name__,
                reason=getattr(exc, "reason", None),
            )
            raise TelegramError(
                TELEGRAM_NOT_CONFIGURED,
                "the Telegram bot token could not be resolved",
            ) from None
        if not isinstance(token, str) or not token.strip():
            raise TelegramError(
                TELEGRAM_NOT_CONFIGURED,
                "the Telegram bot token is not configured",
            )
        try:
            # The stored token is validated on write, but the
            # ``OFT_TELEGRAM_BOT_TOKEN`` fallback is raw; validate here so an
            # invalid-shaped value is never interpolated into the request path.
            return validate_telegram_token(token)
        except TelegramValidationError:
            raise TelegramError(
                TELEGRAM_TOKEN_INVALID,
                "the configured Telegram bot token is invalid",
            ) from None

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        token = self._token()
        url = f"/bot{token}/{method}"
        try:
            response = self._client_or_create().post(url, json=payload)
        except httpx.InvalidURL:
            raise TelegramError(
                TELEGRAM_TOKEN_INVALID,
                "the configured Telegram bot token is invalid",
            ) from None
        except httpx.HTTPError as exc:
            # Never forward the httpx text: it can embed the request URL (and so
            # the token). Only the exception class name is bounded and safe.
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR,
                f"the Telegram Bot API request failed ({type(exc).__name__})",
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR,
                f"the Telegram Bot API returned a non-JSON response (HTTP {response.status_code})",
            ) from None
        if not isinstance(body, dict):
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR,
                "the Telegram Bot API returned an unexpected response "
                f"(HTTP {response.status_code})",
            )
        if body.get("ok") is True:
            return body.get("result")
        error_code = body.get("error_code")
        mapped = _map_error_code(error_code, response.status_code)
        # Only the numeric code is surfaced; the description body is dropped.
        detail = error_code if isinstance(error_code, int) else response.status_code
        raise TelegramError(
            mapped,
            f"the Telegram Bot API rejected {method} (error_code={detail})",
            retry_after=_retry_after(body),
        )

    def get_me(self) -> TelegramIdentity:
        result = self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramError(TELEGRAM_UPSTREAM_ERROR, "the Telegram getMe result was malformed")
        user_id = result.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR, "the Telegram getMe result carried no bot id"
            )
        username = result.get("username")
        return TelegramIdentity(
            user_id=user_id,
            username=username if isinstance(username, str) else None,
        )

    def get_updates(
        self, *, offset: int | None = None, timeout_seconds: int = 0
    ) -> TelegramUpdatePage:
        payload: dict[str, Any] = {"timeout": max(0, timeout_seconds)}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR, "the Telegram getUpdates result was malformed"
            )
        updates: list[TelegramUpdate] = []
        max_update_id: int | None = None
        for raw in result:
            raw_update_id = _raw_update_id(raw)
            if raw_update_id is not None:
                max_update_id = (
                    raw_update_id if max_update_id is None else max(max_update_id, raw_update_id)
                )
            parsed = parse_update(raw)
            if parsed is not None:
                updates.append(parsed)
        return TelegramUpdatePage(updates=tuple(updates), max_update_id=max_update_id)

    def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> SentMessage:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        # Deliberately no ``parse_mode``: replies are plain text so no inbound
        # or rendered value can become Telegram markup.
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR, "the Telegram sendMessage result was malformed"
            )
        message_id = result.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise TelegramError(
                TELEGRAM_UPSTREAM_ERROR, "the Telegram sendMessage result carried no message id"
            )
        return SentMessage(message_id=message_id)


def _map_error_code(error_code: object, http_status: int) -> str:
    """Map the numeric Bot API/HTTP failure to one stable port code."""
    code = error_code if isinstance(error_code, int) else http_status
    # 404 is Telegram's "Not Found" for a bot path: the token was revoked or the
    # bot no longer exists, so it is a permanently invalid token (never a
    # transient upstream failure).
    if code in (401, 404):
        return TELEGRAM_TOKEN_INVALID
    if code == 429:
        return TELEGRAM_RATE_LIMITED
    if code == 409:
        return TELEGRAM_CONFLICT
    return TELEGRAM_UPSTREAM_ERROR


def _raw_update_id(raw: object) -> int | None:
    """The positive integer ``update_id`` of a raw item, else ``None``."""
    if not isinstance(raw, Mapping):
        return None
    update_id = raw.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id <= 0:
        return None
    return update_id


def _retry_after(body: Mapping[str, Any]) -> int | None:
    """The Bot API ``parameters.retry_after`` when it is a non-negative int."""
    parameters = body.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    value = parameters.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = ["BOT_API_HOST", "DEFAULT_LONG_POLL_SECONDS", "TelegramBotApiAdapter"]
