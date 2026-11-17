"""Pure Telegram domain rules: command grammar, sanitization, and DTOs.

The inbound surface is untrusted text. Only a closed token table is executable;
write-like tokens (``/approve`` and friends) are recognized only so they can be
refused. Everything else is ignored. The sanitizer strips C0/C1 controls,
zero-width and bidi characters, collapses whitespace, and bounds both arguments
and outbound messages so no inbound payload can grow without limit.

Nothing here performs I/O, calls a model, or reaches a shell, SQL, or dynamic
dispatch. The bot token shape is validated here so the service, API, and CLI
share one rule.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from oft.domain.settings import SettingValidationError

# The canonical Bot API token shape: numeric bot id, colon, base64url-ish secret.
# Deliberately unbounded: the real leak form is ``/bot<token>/getMe`` (preceded
# by a word character), and a valid secret may end in ``-`` (a non-word char).
# Validation uses ``fullmatch``, so anchoring is still exact.
TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
TELEGRAM_BOT_TOKEN_MAX_LENGTH = 512
MAX_INBOUND_TEXT = 4096
MAX_ARGUMENT_CHARS = 512
MAX_OUTBOUND_CHARS = 4096
MAX_TELEGRAM_CHAT_IDS = 10
MAX_TELEGRAM_ALLOWED_USER_IDS = 20
REFUSAL_WINDOW_SECONDS = 60
MAX_REFUSALS_PER_WINDOW = 3
# A global budget across all chats so attacker-chosen chat ids cannot drive
# unbounded refusal-receipt/outbox growth inside one window. The append-only
# audit trail is bounded separately by wall-clock aggregation, below.
MAX_GLOBAL_REFUSALS_PER_WINDOW = 20
# Sustained unauthenticated refusals must not grow SQLite without bound. Three
# independent bounds apply:
# - the global refusal budget caps refusal receipts/outbox rows per window;
# - unauthorized audit rows are aggregated to at most two rows per wall-clock
#   audit window (an immediate row plus a closing summary), so the append-only
#   ``audit_events`` chain is never deleted but cannot grow at attacker rate;
# - terminal outbox rows older than the retention are purged hourly by the
#   dispatcher in bounded batches (see the purge constants below).
TELEGRAM_REFUSAL_AUDIT_WINDOW_SECONDS = 3600

STATUS_COMMAND = "/status"
POSITIONS_COMMAND = "/positions"
OPPORTUNITIES_COMMAND = "/opportunities"
REPORT_COMMAND = "/report"
BUDGET_COMMAND = "/budget"
WATCHLIST_COMMAND = "/watchlist"
HELP_COMMAND = "/help"
# The executable closed token table. Only these exact tokens dispatch a handler.
COMMANDS: tuple[str, ...] = (
    STATUS_COMMAND,
    POSITIONS_COMMAND,
    OPPORTUNITIES_COMMAND,
    REPORT_COMMAND,
    BUDGET_COMMAND,
    WATCHLIST_COMMAND,
    HELP_COMMAND,
)
# Recognized but deliberately not executable: each returns one fixed refusal and
# has no mutation branch anywhere in the service.
WRITE_LIKE_COMMANDS: tuple[str, ...] = (
    "/approve",
    "/accept",
    "/reject",
    "/cancel",
    "/buy",
    "/sell",
    "/close",
)
REFUSAL_TEXT = "未授权"
WRITE_REFUSAL_TEXT = "MVP 只读，请在桌面端或 CLI 操作"
SYMBOL_ARGUMENT_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
TRUNCATION_MARKER = "…"
DESKTOP_VIEW_TEXT = "更多内容请在桌面端查看"

TELEGRAM_CHAT_IDS_KEY = "telegram.chat_ids"
TELEGRAM_ALLOWED_USER_IDS_KEY = "telegram.allowed_user_ids"
POLL_OFFSET_KEY = "telegram.poll_offset"
POLLER_HEARTBEAT_KEY = "telegram.poller_heartbeat"
DISPATCHER_HEARTBEAT_KEY = "telegram.dispatcher_heartbeat"
POLLER_STALE_SECONDS = 60
# The single retry bound shared by the claim predicate and the terminal
# decision in the dispatcher, so the two layers cannot drift.
MAX_TELEGRAM_OUTBOX_ATTEMPTS = 3
# A row claimed as 'sending' is re-claimed (and re-sent) only after this lease
# expires, so a crash during the send cannot strand the reply forever. Must
# exceed the adapter's 35 s HTTP timeout.
TELEGRAM_OUTBOX_CLAIM_LEASE_SECONDS = 120
TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS = 2
TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS = 300
TELEGRAM_RETRY_AFTER_MAX_SECONDS = 3600
# A non-counted retry (NOT_CONFIGURED / unhelpful RATE_LIMITED) must still
# terminate: once the row is this old it becomes terminal ``failed`` instead of
# retrying forever. Until then its delay grows with the row's age.
TELEGRAM_OUTBOX_DEFER_MAX_SECONDS = 3600
# Terminal outbox rows older than this are purged periodically (bounded
# batches, at most once per interval) so a long-running daemon cannot grow the
# table without bound.
TELEGRAM_OUTBOX_RETENTION_SECONDS = 7 * 24 * 3600
TELEGRAM_OUTBOX_PURGE_KEY = "telegram.outbox_purge_at"
TELEGRAM_OUTBOX_PURGE_INTERVAL_SECONDS = 3600
TELEGRAM_OUTBOX_PURGE_BATCH = 1000
TELEGRAM_OUTBOX_PURGE_MAX_BATCHES = 10

TELEGRAM_SUBJECT = "telegram"
TELEGRAM_TOKEN_NAME = "bot_token"


class TelegramValidationError(SettingValidationError):
    """A Telegram token or allowlist value failed typed validation."""


class ReceiptState(StrEnum):
    """The persisted outbox/receipt states."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class TelegramAllowlist:
    """The two bounded operator allowlists."""

    chat_ids: tuple[int, ...] = ()
    allowed_user_ids: tuple[int, ...] = ()

    def to_contract(self) -> dict[str, Any]:
        return {
            "chat_ids": list(self.chat_ids),
            "allowed_user_ids": list(self.allowed_user_ids),
        }


@dataclass(frozen=True)
class TelegramUpdate:
    """One normalized inbound message; never raw Bot API JSON."""

    update_id: int
    chat_id: int
    user_id: int
    text: str
    message_id: int | None = None


@dataclass(frozen=True)
class TelegramCommand:
    """A parsed command from the closed token table."""

    name: str
    argument: str = ""


def _detail(field: str, code: str, message: str) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _nonzero_int(value: object) -> int | None:
    """A signed non-zero integer id; group/supergroup chat ids are negative."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value != 0 else None


def sanitize_inbound_text(value: object) -> str:
    """Strip controls/zero-width/bidi chars, collapse whitespace, cap length."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        character
        for character in unicodedata.normalize("NFC", value)
        if not unicodedata.category(character).startswith("C")
    )
    return " ".join(cleaned.split())[:MAX_INBOUND_TEXT]


def sanitize_argument(value: object) -> str:
    """A bounded argument string; the grammar never passes it to a handler."""
    return sanitize_inbound_text(value)[:MAX_ARGUMENT_CHARS]


def parse_command(text: object) -> TelegramCommand | None:
    """Match the sanitized text against the closed command table exactly.

    The first whitespace-delimited token must be one of the executable commands,
    so a ``/status`` embedded in prose never dispatches. An argument longer than
    ``MAX_ARGUMENT_CHARS`` makes the whole command overlong and is ignored.
    Whether arguments are accepted is decided by the caller; only ``/report``
    accepts one optional symbol.
    """
    cleaned = sanitize_inbound_text(text)
    if not cleaned:
        return None
    parts = cleaned.split(" ")
    name = parts[0]
    if name not in COMMANDS:
        return None
    raw_argument = " ".join(parts[1:])
    if len(raw_argument) > MAX_ARGUMENT_CHARS:
        return None
    return TelegramCommand(name=name, argument=sanitize_argument(raw_argument))


def parse_write_like_command(text: object) -> str | None:
    """Return the exact write-like token, or ``None`` when none leads the text.

    Like :func:`parse_command`, only the first whitespace-delimited token counts,
    so prose that merely mentions ``/approve`` is ignored. A recognized token is
    answered with the fixed read-only refusal and never dispatched.
    """
    cleaned = sanitize_inbound_text(text)
    if not cleaned:
        return None
    token = cleaned.split(" ", 1)[0]
    return token if token in WRITE_LIKE_COMMANDS else None


def normalize_symbol_argument(value: object) -> str | None:
    """Normalize one optional ``/report`` symbol argument, or refuse it.

    The value is sanitized (controls/zero-width/bidi stripped), uppercased, and
    must be exactly one token matching ``^[A-Z0-9]{2,24}$``. An empty, multi-token,
    or markup-bearing value is invalid and never reaches a read.
    """
    cleaned = sanitize_inbound_text(value)
    if not cleaned or " " in cleaned:
        return None
    normalized = cleaned.upper()
    return normalized if SYMBOL_ARGUMENT_PATTERN.fullmatch(normalized) is not None else None


def query_reference_id(*parts: object) -> str:
    """A deterministic, non-secret reference id for one query reply or error."""
    material = ":".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8].upper()
    return f"TG-{digest}"


def truncate_outbound(text: str) -> str:
    """Bound an outbound message to the Bot API text limit."""
    return text[:MAX_OUTBOUND_CHARS]


def budget_outbound(text: str, reference_id: str) -> str:
    """Bound a reply to one message, naming the desktop view when truncated.

    The ``…`` marker and the reference-id line are always retained and the whole
    result stays within :data:`MAX_OUTBOUND_CHARS`. The caller enqueues exactly
    one outbox row; a long render is never split across messages.
    """
    if len(text) <= MAX_OUTBOUND_CHARS:
        return text
    suffix = f"\n{TRUNCATION_MARKER}\n{DESKTOP_VIEW_TEXT}。\nreference_id: {reference_id}"
    room = MAX_OUTBOUND_CHARS - len(suffix)
    if room <= 0:  # pragma: no cover - the suffix is far below the cap
        return suffix[-MAX_OUTBOUND_CHARS:]
    return text[:room].rstrip() + suffix


def is_authorized(chat_id: int, user_id: int, allowlist: TelegramAllowlist) -> bool:
    """Whether one sender may query this bot (fail-closed for users).

    Both lists must be configured: the chat must be allowlisted **and** the
    sender's user id must appear in a non-empty user allowlist. A chat-only
    configuration authorizes nobody, so adding a group chat does not implicitly
    expose the read-only status to every member of that group.
    """
    if not allowlist.allowed_user_ids:
        return False
    return chat_id in allowlist.chat_ids and user_id in allowlist.allowed_user_ids


def parse_update(raw: object) -> TelegramUpdate | None:
    """Normalize one Bot API update; non-text or malformed updates are skipped.

    ``update_id``, ``user_id``, and ``message_id`` are positive; ``chat_id`` is
    any non-zero signed integer so group/supergroup (negative) chats parse.
    """
    if not isinstance(raw, Mapping):
        return None
    update_id = _positive_int(raw.get("update_id"))
    message = raw.get("message")
    if update_id is None or not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
        return None
    chat_id = _nonzero_int(chat.get("id"))
    user_id = _positive_int(sender.get("id"))
    if chat_id is None or user_id is None or not isinstance(text, str):
        return None
    message_id = _positive_int(message.get("message_id"))
    return TelegramUpdate(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        message_id=message_id,
    )


def reply_event_key(update_id: int) -> str:
    """The idempotency key for the one reply to an inbound update."""
    return f"update:{update_id}:reply"


def test_event_key(chat_id: int, nonce: str) -> str:
    """The idempotency key for one operator-triggered test message."""
    return f"test:{chat_id}:{nonce}"


def validate_telegram_token(value: object) -> str:
    """Normalize one bot token; an invalid value is never echoed."""
    if not isinstance(value, str):
        raise TelegramValidationError(
            "bot_token must be a string",
            details=[_detail("bot_token", "not_a_string", "bot_token must be a string")],
        )
    normalized = value.strip()
    if not normalized:
        raise TelegramValidationError(
            "bot_token is required",
            details=[_detail("bot_token", "required", "bot_token is required")],
        )
    if len(normalized) > TELEGRAM_BOT_TOKEN_MAX_LENGTH:
        message = f"bot_token must be at most {TELEGRAM_BOT_TOKEN_MAX_LENGTH} characters"
        raise TelegramValidationError(message, details=[_detail("bot_token", "too_long", message)])
    if any(
        unicodedata.category(character).startswith("C") or character.isspace()
        for character in normalized
    ):
        message = "bot_token must not contain whitespace or control characters"
        raise TelegramValidationError(
            message, details=[_detail("bot_token", "invalid_characters", message)]
        )
    if TELEGRAM_BOT_TOKEN_PATTERN.fullmatch(normalized) is None:
        message = "bot_token does not match the Telegram Bot API token shape"
        raise TelegramValidationError(
            message, details=[_detail("bot_token", "invalid_format", message)]
        )
    return normalized


__all__ = [
    "BUDGET_COMMAND",
    "COMMANDS",
    "DESKTOP_VIEW_TEXT",
    "DISPATCHER_HEARTBEAT_KEY",
    "HELP_COMMAND",
    "MAX_ARGUMENT_CHARS",
    "MAX_GLOBAL_REFUSALS_PER_WINDOW",
    "MAX_INBOUND_TEXT",
    "MAX_OUTBOUND_CHARS",
    "MAX_REFUSALS_PER_WINDOW",
    "MAX_TELEGRAM_ALLOWED_USER_IDS",
    "MAX_TELEGRAM_CHAT_IDS",
    "MAX_TELEGRAM_OUTBOX_ATTEMPTS",
    "OPPORTUNITIES_COMMAND",
    "POLLER_HEARTBEAT_KEY",
    "POLLER_STALE_SECONDS",
    "POLL_OFFSET_KEY",
    "POSITIONS_COMMAND",
    "REFUSAL_TEXT",
    "REFUSAL_WINDOW_SECONDS",
    "REPORT_COMMAND",
    "STATUS_COMMAND",
    "SYMBOL_ARGUMENT_PATTERN",
    "TELEGRAM_ALLOWED_USER_IDS_KEY",
    "TELEGRAM_BOT_TOKEN_MAX_LENGTH",
    "TELEGRAM_BOT_TOKEN_PATTERN",
    "TELEGRAM_CHAT_IDS_KEY",
    "TELEGRAM_OUTBOX_BACKOFF_BASE_SECONDS",
    "TELEGRAM_OUTBOX_BACKOFF_MAX_SECONDS",
    "TELEGRAM_OUTBOX_CLAIM_LEASE_SECONDS",
    "TELEGRAM_OUTBOX_DEFER_MAX_SECONDS",
    "TELEGRAM_OUTBOX_PURGE_BATCH",
    "TELEGRAM_OUTBOX_PURGE_INTERVAL_SECONDS",
    "TELEGRAM_OUTBOX_PURGE_KEY",
    "TELEGRAM_OUTBOX_PURGE_MAX_BATCHES",
    "TELEGRAM_OUTBOX_RETENTION_SECONDS",
    "TELEGRAM_REFUSAL_AUDIT_WINDOW_SECONDS",
    "TELEGRAM_RETRY_AFTER_MAX_SECONDS",
    "TELEGRAM_SUBJECT",
    "TELEGRAM_TOKEN_NAME",
    "TRUNCATION_MARKER",
    "WATCHLIST_COMMAND",
    "WRITE_LIKE_COMMANDS",
    "WRITE_REFUSAL_TEXT",
    "ReceiptState",
    "TelegramAllowlist",
    "TelegramCommand",
    "TelegramUpdate",
    "TelegramValidationError",
    "budget_outbound",
    "is_authorized",
    "normalize_symbol_argument",
    "parse_command",
    "parse_update",
    "parse_write_like_command",
    "query_reference_id",
    "reply_event_key",
    "sanitize_argument",
    "sanitize_inbound_text",
    "test_event_key",
    "truncate_outbound",
    "validate_telegram_token",
]
