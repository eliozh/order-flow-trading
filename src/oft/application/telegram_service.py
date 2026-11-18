"""Telegram application service: authorization, dispatch, enqueue, receipts.

The service is the only place that decides whether an inbound sender may query
the bot. It sanitizes untrusted text, matches a closed command table, renders a
read-only reply, and appends exactly one outbox row per update. It never calls
an LLM, a shell, SQL, or a dynamic dispatcher, and it never exposes the token.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog

from oft.application import telegram_queries
from oft.application.secrets_service import SecretsService
from oft.application.settings_service import (
    SettingsService,
    TelegramAllowlistSettings,
    TelegramSettings,
)
from oft.application.status import build_status
from oft.application.telegram_messages import (
    render_help,
    render_status,
    render_test_message,
)
from oft.config import Settings
from oft.domain.telegram import (
    BUDGET_COMMAND,
    HELP_COMMAND,
    MAX_GLOBAL_REFUSALS_PER_WINDOW,
    MAX_REFUSALS_PER_WINDOW,
    OPPORTUNITIES_COMMAND,
    POSITIONS_COMMAND,
    REFUSAL_TEXT,
    REFUSAL_WINDOW_SECONDS,
    REPORT_COMMAND,
    STATUS_COMMAND,
    TELEGRAM_REFUSAL_AUDIT_WINDOW_SECONDS,
    WATCHLIST_COMMAND,
    WRITE_REFUSAL_TEXT,
    ReceiptState,
    TelegramAllowlist,
    TelegramCommand,
    TelegramUpdate,
    TelegramValidationError,
    budget_outbound,
    is_authorized,
    normalize_symbol_argument,
    parse_command,
    parse_write_like_command,
    query_reference_id,
    reply_event_key,
    sanitize_inbound_text,
    test_event_key,
    validate_telegram_token,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import AuditEventDraft, StorePort, TelegramOutboxRecord
from oft.ports.telegram import (
    TELEGRAM_NOT_CONFIGURED,
    TelegramError,
    TelegramPort,
)

LOGGER = structlog.get_logger(__name__)
TelegramState = Literal["NOT_CONFIGURED", "NO_ALLOWLIST", "READY", "DEGRADED"]
_UNAUTHORIZED_EVENT = "TELEGRAM_UNAUTHORIZED"
# The refusal window is per chat; this caps the number of chat entries kept so
# a flood of distinct unknown chats cannot grow the in-memory map without bound.
MAX_REFUSAL_CHATS = 1024


@dataclass(frozen=True)
class TelegramTokenStatus:
    """The only token-shaped data a surface may render."""

    configured: bool
    masked: str | None
    last4: str | None
    source: str
    updated_at: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "masked": self.masked,
            "last4": self.last4,
            "source": self.source,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class TelegramStatus:
    """The deterministic channel state (never a network probe)."""

    state: TelegramState
    token_configured: bool
    chat_count: int
    allowed_user_count: int
    detail: str

    def to_contract(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "token_configured": self.token_configured,
            "chat_count": self.chat_count,
            "allowed_user_count": self.allowed_user_count,
            "detail": self.detail,
        }


def receipt_contract(record: TelegramOutboxRecord) -> dict[str, Any]:
    """The public receipt shape; the payload text is never echoed here."""
    return {
        "event_key": record.event_key,
        "kind": record.kind,
        "chat_id": record.chat_id,
        "reply_to_message_id": record.reply_to_message_id,
        "state": record.state,
        "attempts": record.attempts,
        "message_id": record.message_id,
        "last_error_code": record.last_error_code,
        "next_attempt_at": record.next_attempt_at,
        "created_at": record.created_at,
        "sent_at": record.sent_at,
    }


class TelegramService:
    """Authorize, render, and enqueue Telegram replies."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StorePort,
        clock: ClockPort,
        market_data: MarketDataPort,
        secrets: SecretsService,
        settings_service: SettingsService,
        port: TelegramPort,
        refusal_window_seconds: int = REFUSAL_WINDOW_SECONDS,
        max_refusals: int = MAX_REFUSALS_PER_WINDOW,
        max_global_refusals: int = MAX_GLOBAL_REFUSALS_PER_WINDOW,
        max_refusal_chats: int = MAX_REFUSAL_CHATS,
        refusal_audit_window_seconds: int = TELEGRAM_REFUSAL_AUDIT_WINDOW_SECONDS,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._market_data = market_data
        self._secrets = secrets
        self._settings_service = settings_service
        self._port = port
        self._refusal_window_seconds = refusal_window_seconds
        self._max_refusals = max_refusals
        self._max_global_refusals = max(1, max_global_refusals)
        self._max_refusal_chats = max(1, max_refusal_chats)
        self._refusal_audit_window_seconds = max(1, refusal_audit_window_seconds)
        self._refusals: dict[int, list[datetime]] = {}
        self._global_refusals: list[datetime] = []
        self._audit_window_started_at: datetime | None = None
        self._audit_count = 0
        self._audit_first_update_id: int | None = None
        self._audit_last_update_id: int | None = None
        self._refusal_lock = threading.Lock()

    # -- token and allowlist --------------------------------------------

    def token_status(self) -> TelegramTokenStatus:
        status = self._secrets.status_for("telegram", "bot_token")
        return TelegramTokenStatus(
            configured=status.configured,
            masked=status.masked,
            last4=status.last4,
            source=status.key_source,
            updated_at=status.updated_at,
        )

    def set_token(self, value: object, *, actor: str) -> TelegramTokenStatus:
        self._secrets.set_secret("telegram", "bot_token", value, actor=actor)
        return self.token_status()

    def delete_token(self, *, actor: str) -> TelegramTokenStatus:
        self._secrets.delete_secret("telegram", "bot_token", actor=actor)
        return self.token_status()

    def allowlist(self) -> TelegramAllowlistSettings:
        return self._settings_service.read_telegram_allowlist()

    def settings_state(self) -> TelegramSettings:
        """The allowlists, six push toggles, and their provenance (no token)."""
        return self._settings_service.read_telegram_settings()

    def update_settings(
        self,
        *,
        chat_ids: object = None,
        allowed_user_ids: object = None,
        toggles: object = None,
        actor: str,
        reason: str = "",
    ) -> TelegramSettings:
        """Delegate a partial Telegram settings patch to the shared service."""
        changes: dict[str, object] = {}
        if chat_ids is not None:
            changes["chat_ids"] = chat_ids
        if allowed_user_ids is not None:
            changes["allowed_user_ids"] = allowed_user_ids
        if toggles is not None:
            changes["toggles"] = toggles
        return self._settings_service.update_telegram_settings(
            **changes,
            actor=actor,
            reason=reason,
        )

    def latest_test_receipt(self) -> TelegramOutboxRecord | None:
        """The newest test receipt, or ``None``.

        Bounded to the newest 100 rows; receipts are small and a test send is an
        explicit operator action, so no dedicated query is warranted.
        """
        for record in self.receipts(limit=100):
            if record.kind == "test":
                return record
        return None

    def update_allowlist(
        self,
        chat_ids: object,
        allowed_user_ids: object,
        *,
        actor: str,
        reason: str = "",
    ) -> TelegramAllowlistSettings:
        return self._settings_service.update_telegram_allowlist(
            chat_ids,
            allowed_user_ids,
            actor=actor,
            reason=reason,
        )

    def status_state(self) -> TelegramStatus:
        token = self.token_status()
        allowlist = self.allowlist()
        if token.source == "unavailable":
            state: TelegramState = "DEGRADED"
            detail = "the secure secret store is unavailable"
        elif not token.configured:
            state = "NOT_CONFIGURED"
            detail = "the Telegram bot token is not configured"
        elif token.source == "env" and self._env_token_is_invalid():
            # A raw environment value is not validated on write, so an
            # invalid-shaped token must not report READY.
            state = "DEGRADED"
            detail = "the environment Telegram bot token is invalid"
        elif not allowlist.chat_ids:
            state = "NO_ALLOWLIST"
            detail = "no Telegram chat is allowlisted"
        elif not allowlist.allowed_user_ids:
            # Fail-closed: the authorizer requires both lists, so a chat-only
            # configuration is not ready and refuses every sender.
            state = "NO_ALLOWLIST"
            detail = "no Telegram user is allowlisted; a chat-only allowlist refuses every sender"
        else:
            state = "READY"
            detail = "the Telegram channel is ready"
        return TelegramStatus(
            state=state,
            token_configured=token.configured,
            chat_count=len(allowlist.chat_ids),
            allowed_user_count=len(allowlist.allowed_user_ids),
            detail=detail,
        )

    # -- inbound ---------------------------------------------------------

    def _env_token_is_invalid(self) -> bool:
        """Whether the raw environment token fails the shared shape rule."""
        token = self._settings.telegram_bot_token
        if token is None:
            return False
        getter = getattr(token, "get_secret_value", None)
        value = getter() if callable(getter) else token
        try:
            validate_telegram_token(value)
        except TelegramValidationError:
            return True
        return False

    def handle_update(self, update: TelegramUpdate) -> TelegramOutboxRecord | None:
        """Authorize one update, then enqueue at most one reply.

        An unknown chat/user receives the fixed ``未授权`` refusal (rate-limited
        and audited) and no data, before any parse or read-model work. An
        authorized write-like token (``/approve`` and friends) receives the
        fixed read-only refusal and has no mutation branch. A prose-embedded or
        unknown command is ignored entirely; only an exact allowlisted token
        dispatches, and only ``/report`` accepts one optional symbol argument.
        """
        text = sanitize_inbound_text(update.text)
        allowlist = self.allowlist()
        authorization = TelegramAllowlist(
            chat_ids=allowlist.chat_ids,
            allowed_user_ids=allowlist.allowed_user_ids,
        )
        now = self._clock.now()
        if not is_authorized(update.chat_id, update.user_id, authorization):
            # Every unauthorized update is accounted for -- including those the
            # budgets drop -- but the append-only audit chain is aggregated per
            # wall-clock window instead of growing at attacker rate.
            for draft in self._refusal_audit_drafts(update, now):
                self._store.append_audit_event(draft)
            if not self._refusal_allowed(update.chat_id, now):
                return None
            return self._enqueue(
                event_key=reply_event_key(update.update_id),
                kind="refusal",
                chat_id=update.chat_id,
                reply_to_message_id=update.message_id,
                text=REFUSAL_TEXT,
                priority=10,
                now=now,
            )
        if parse_write_like_command(text) is not None:
            return self._enqueue(
                event_key=reply_event_key(update.update_id),
                kind="refusal",
                chat_id=update.chat_id,
                reply_to_message_id=update.message_id,
                text=WRITE_REFUSAL_TEXT,
                priority=10,
                now=now,
            )
        command = parse_command(text)
        if command is None:
            return None
        if command.name != REPORT_COMMAND and command.argument:
            # Only ``/report`` accepts an argument; every other command ignores
            # an argument entirely instead of dispatching a partial query.
            return None
        reference_id = query_reference_id(update.update_id, command.name)
        reply = self._render_query(command, reference_id=reference_id)
        if reply is None:  # pragma: no cover - the parser returns only the table
            return None
        return self._enqueue(
            event_key=reply_event_key(update.update_id),
            kind="query_reply",
            chat_id=update.chat_id,
            reply_to_message_id=update.message_id,
            text=budget_outbound(reply, reference_id),
            priority=0,
            now=now,
        )

    def _render_query(self, command: TelegramCommand, *, reference_id: str) -> str | None:
        """Render one allowlisted query over the existing read models."""
        if command.name == STATUS_COMMAND:
            report = build_status(
                self._settings,
                self._market_data,
                self._clock,
                store=self._store,
            )
            return render_status(report)
        if command.name == HELP_COMMAND:
            return render_help()
        if command.name == POSITIONS_COMMAND:
            return telegram_queries.render_positions_query(
                store=self._store,
                clock=self._clock,
                settings=self._settings,
                reference_id=reference_id,
            )
        if command.name == OPPORTUNITIES_COMMAND:
            return telegram_queries.render_opportunities_query(
                store=self._store,
                clock=self._clock,
                reference_id=reference_id,
            )
        if command.name == REPORT_COMMAND:
            if not command.argument:
                return telegram_queries.render_report_query(
                    store=self._store,
                    symbol=None,
                    reference_id=reference_id,
                )
            symbol = normalize_symbol_argument(command.argument)
            if symbol is None:
                return telegram_queries.render_invalid_argument_error(reference_id)
            return telegram_queries.render_report_query(
                store=self._store,
                symbol=symbol,
                reference_id=reference_id,
            )
        if command.name == BUDGET_COMMAND:
            return telegram_queries.render_budget_query(
                store=self._store,
                clock=self._clock,
                settings=self._settings,
                reference_id=reference_id,
            )
        if command.name == WATCHLIST_COMMAND:
            return telegram_queries.render_watchlist_query(
                store=self._store,
                clock=self._clock,
                reference_id=reference_id,
            )
        return None

    def _enqueue(
        self,
        *,
        event_key: str,
        kind: str,
        chat_id: int,
        reply_to_message_id: int | None,
        text: str,
        priority: int,
        now: datetime,
    ) -> TelegramOutboxRecord:
        timestamp = utc_timestamp(now)
        record = TelegramOutboxRecord(
            event_key=event_key,
            kind=kind,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            payload_json=json.dumps({"text": text}),
            priority=priority,
            state=ReceiptState.QUEUED,
            attempts=0,
            next_attempt_at=timestamp,
            message_id=None,
            last_error_code=None,
            created_at=timestamp,
            sent_at=None,
        )
        self._store.append_telegram_outbox(record)
        return record

    def _refusal_allowed(self, chat_id: int, now: datetime) -> bool:
        """Whether to enqueue a refusal receipt for this chat.

        Two bounds apply: a per-chat window (at most ``max_refusals`` receipts)
        and a global window across all chats. A refusal beyond the global budget
        is dropped silently, so attacker-chosen chat ids cannot drive unbounded
        receipt/outbox growth. Audit accounting is separate and lives in
        :meth:`_refusal_audit_drafts`.
        """
        with self._refusal_lock:
            self._global_refusals = [
                seen
                for seen in self._global_refusals
                if (now - seen).total_seconds() < self._refusal_window_seconds
            ]
            if len(self._global_refusals) >= self._max_global_refusals:
                return False
            if chat_id not in self._refusals and len(self._refusals) >= self._max_refusal_chats:
                # Evict the oldest chat entry; its window will start afresh.
                self._refusals.pop(next(iter(self._refusals)), None)
            history = [
                seen
                for seen in self._refusals.get(chat_id, ())
                if (now - seen).total_seconds() < self._refusal_window_seconds
            ]
            if len(history) >= self._max_refusals:
                self._refusals[chat_id] = history
                return False
            history.append(now)
            self._refusals[chat_id] = history
            self._global_refusals.append(now)
            return True

    def _refusal_audit_drafts(
        self, update: TelegramUpdate, now: datetime
    ) -> tuple[AuditEventDraft, ...]:
        """Aggregate unauthorized updates into at most two rows per window.

        The first refusal of a window is written immediately so any single
        refusal stays visible. Later refusals only bump an in-memory counter;
        when the wall-clock window closes, one neutral summary row describes the
        closing window (when it saw more than one refusal) alongside the new
        window's immediate row. Accounting happens under ``_refusal_lock`` for
        every unauthorized update, including those the refusal budgets drop, so
        attacker volume cannot grow the append-only audit chain without bound.
        """
        with self._refusal_lock:
            started = self._audit_window_started_at
            if started is None:
                self._start_audit_window(update, now)
                return (self._immediate_audit_draft(update, now),)
            if (now - started).total_seconds() < self._refusal_audit_window_seconds:
                self._audit_count += 1
                self._audit_last_update_id = update.update_id
                return ()
            drafts: list[AuditEventDraft] = []
            if self._audit_count > 1:
                drafts.append(self._aggregate_audit_draft(started, now))
            self._start_audit_window(update, now)
            drafts.append(self._immediate_audit_draft(update, now))
            return tuple(drafts)

    def _start_audit_window(self, update: TelegramUpdate, now: datetime) -> None:
        self._audit_window_started_at = now
        self._audit_count = 1
        self._audit_first_update_id = update.update_id
        self._audit_last_update_id = update.update_id

    def _immediate_audit_draft(self, update: TelegramUpdate, now: datetime) -> AuditEventDraft:
        return AuditEventDraft(
            event_type=_UNAUTHORIZED_EVENT,
            actor={"kind": "TELEGRAM", "id": str(update.user_id)},
            subject={"kind": "TELEGRAM", "id": str(update.chat_id)},
            payload={
                "count": 1,
                "first_update_id": update.update_id,
                "last_update_id": update.update_id,
                "window_start": utc_timestamp(now),
                "aggregated": False,
            },
            created_at=utc_timestamp(now),
        )

    def _aggregate_audit_draft(self, started: datetime, now: datetime) -> AuditEventDraft:
        # A neutral actor/subject: the summary spans many chats/users, so it must
        # not be attributed to any single one.
        return AuditEventDraft(
            event_type=_UNAUTHORIZED_EVENT,
            actor={"kind": "TELEGRAM", "id": "aggregate"},
            subject={"kind": "TELEGRAM", "id": "aggregate"},
            payload={
                "count": self._audit_count,
                "first_update_id": self._audit_first_update_id,
                "last_update_id": self._audit_last_update_id,
                "window_start": utc_timestamp(started),
                "window_seconds": self._refusal_audit_window_seconds,
                "aggregated": True,
            },
            created_at=utc_timestamp(now),
        )

    # -- operator test send and receipts ---------------------------------

    def enqueue_test(self, *, actor: str, chat_id: int | None = None) -> TelegramOutboxRecord:
        """Validate the token/chat, enqueue one test message, return its receipt."""
        if not self.token_status().configured:
            raise TelegramError(TELEGRAM_NOT_CONFIGURED, "the Telegram bot token is not configured")
        # A real token probe: a 401 becomes TELEGRAM_TOKEN_INVALID at the port.
        self._port.get_me()
        allowlist = self.allowlist()
        target = (
            chat_id
            if chat_id is not None
            else (allowlist.chat_ids[0] if allowlist.chat_ids else None)
        )
        if target is None:
            raise TelegramError(TELEGRAM_NOT_CONFIGURED, "no Telegram chat is allowlisted")
        if target not in allowlist.chat_ids:
            raise TelegramError(
                TELEGRAM_NOT_CONFIGURED, "the requested Telegram chat is not allowlisted"
            )
        now = self._clock.now()
        nonce = f"{int(now.timestamp() * 1000)}-{uuid.uuid4().hex}"
        return self._enqueue(
            event_key=test_event_key(target, nonce),
            kind="test",
            chat_id=target,
            reply_to_message_id=None,
            text=render_test_message(utc_timestamp(now)),
            priority=5,
            now=now,
        )

    def receipts(self, limit: int = 20) -> tuple[TelegramOutboxRecord, ...]:
        return self._store.list_telegram_receipts(limit)

    def receipt_count(self) -> int:
        return self._store.count_telegram_receipts()


__all__ = [
    "TelegramService",
    "TelegramStatus",
    "TelegramTokenStatus",
    "receipt_contract",
]
