"""Telegram API request/response schemas (slice 47).

Token responses are masked metadata only: ``masked``, ``last4``, ``source``, and
``updated_at``; there is deliberately no ``value`` field and no reveal route.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oft.api.schemas.envelope import Envelope
from oft.api.schemas.errors import ErrorBody, ErrorEnvelope
from oft.api.schemas.settings import ERROR_RESPONSES
from oft.domain.telegram import ReceiptState

KeySource = Literal["env", "keyring", "encrypted_file", "stored", "unset", "unavailable"]
TelegramState = Literal["NOT_CONFIGURED", "NO_ALLOWLIST", "READY", "DEGRADED"]


class TelegramTokenUpdate(BaseModel):
    """The write-only token payload; it is never echoed in a response."""

    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=1, max_length=512)


class TelegramTokenDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelegramTokenStatusData(BaseModel):
    configured: bool
    masked: str | None = None
    last4: str | None = None
    source: KeySource
    updated_at: str | None = None


class TelegramAllowlistUpdate(BaseModel):
    """Both allowlists; the service validates count/shape and sorts them."""

    model_config = ConfigDict(extra="forbid")

    chat_ids: list[int]
    allowed_user_ids: list[int]
    reason: str = Field(default="", max_length=500)


class TelegramAllowlistData(BaseModel):
    chat_ids: list[int]
    allowed_user_ids: list[int]
    updated_at: str | None = None
    updated_by: str | None = None


class TelegramStatusData(BaseModel):
    state: TelegramState
    token_configured: bool
    chat_count: int
    allowed_user_count: int
    detail: str = ""


class TelegramTestRequest(BaseModel):
    """An optional explicit chat; omitted means the first allowlisted chat."""

    model_config = ConfigDict(extra="forbid")

    chat_id: int | None = None


class TelegramReceiptData(BaseModel):
    event_key: str
    kind: str
    chat_id: int
    reply_to_message_id: int | None = None
    state: ReceiptState
    attempts: int
    message_id: int | None = None
    last_error_code: str | None = None
    next_attempt_at: str
    created_at: str
    sent_at: str | None = None


class TelegramReceiptsData(BaseModel):
    items: list[TelegramReceiptData]
    total: int


__all__ = [
    "ERROR_RESPONSES",
    "Envelope",
    "ErrorBody",
    "ErrorEnvelope",
    "TelegramAllowlistData",
    "TelegramAllowlistUpdate",
    "TelegramReceiptData",
    "TelegramReceiptsData",
    "TelegramState",
    "TelegramStatusData",
    "TelegramTestRequest",
    "TelegramTokenDelete",
    "TelegramTokenStatusData",
    "TelegramTokenUpdate",
]
