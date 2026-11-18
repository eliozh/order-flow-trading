"""Telegram API request/response schemas (slice 47).

Token responses are masked metadata only: ``masked``, ``last4``, ``source``, and
``updated_at``; there is deliberately no ``value`` field and no reveal route.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

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


class TelegramTogglesData(BaseModel):
    """The six operator-facing push toggles (defaults are all enabled)."""

    scan_report: bool
    trigger: bool
    entry_recheck: bool
    position: bool
    risk: bool
    budget: bool


class TelegramTogglesUpdate(BaseModel):
    """A partial toggle patch; an omitted toggle keeps its stored value."""

    model_config = ConfigDict(extra="forbid")

    scan_report: bool | None = None
    trigger: bool | None = None
    entry_recheck: bool | None = None
    position: bool | None = None
    risk: bool | None = None
    budget: bool | None = None


class TelegramSettingsUpdate(BaseModel):
    """A partial Telegram settings patch; omitted fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    chat_ids: list[StrictInt] | None = None
    allowed_user_ids: list[StrictInt] | None = None
    toggles: TelegramTogglesUpdate | None = None
    reason: str = Field(default="", max_length=500)


class TelegramSettingsData(BaseModel):
    """The aggregate F10f payload: masked token plus configuration/receipts."""

    token: TelegramTokenStatusData
    chat_ids: list[int]
    allowed_user_ids: list[int]
    toggles: TelegramTogglesData
    source: Literal["default", "user"]
    updated_at: str | None = None
    updated_by: str | None = None
    receipts: list[TelegramReceiptData]
    receipt_total: int
    latest_test_receipt: TelegramReceiptData | None = None


__all__ = [
    "ERROR_RESPONSES",
    "Envelope",
    "ErrorBody",
    "ErrorEnvelope",
    "TelegramAllowlistData",
    "TelegramAllowlistUpdate",
    "TelegramReceiptData",
    "TelegramReceiptsData",
    "TelegramSettingsData",
    "TelegramSettingsUpdate",
    "TelegramState",
    "TelegramStatusData",
    "TelegramTestRequest",
    "TelegramTogglesData",
    "TelegramTogglesUpdate",
    "TelegramTokenDelete",
    "TelegramTokenStatusData",
    "TelegramTokenUpdate",
]
