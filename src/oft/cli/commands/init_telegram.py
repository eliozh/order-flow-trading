"""Telegram step 4 and flag handling for ``oft init`` (slice 53, S01.4).

This module is a thin CLI adapter extracted from
:mod:`oft.cli.commands.init` to keep the wizard module within the project's
file-size ceiling. Every read and write still goes through
:class:`~oft.application.telegram_service.TelegramService` and the shared
:func:`~oft.application.settings_service.normalize_telegram_ids` validator, so
the CLI never re-implements the allowlist rules or persists settings itself.

The optional step is resumable: skipping it writes nothing and never completes
it, and a run may configure it after the required steps already finished.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import typer

from oft.application.onboarding_service import ONBOARDING_STEPS
from oft.application.settings_service import SettingsService, normalize_telegram_ids
from oft.application.telegram_service import TelegramService
from oft.config import ConfigError
from oft.domain.telegram import (
    MAX_TELEGRAM_ALLOWED_USER_IDS,
    MAX_TELEGRAM_CHAT_IDS,
    TELEGRAM_BOT_TOKEN_MAX_LENGTH,
    TelegramValidationError,
)
from oft.ports.telegram import TelegramError

# The shared step definition owns the title; never duplicate it here.
_TELEGRAM_STEP_TITLE = next(step.title for step in ONBOARDING_STEPS if step.step == 4)


class PromptText(Protocol):
    """The visible prompt seam supplied by the wizard (patched by tests)."""

    def __call__(self, label: str, *, default: str = "") -> str: ...


@dataclass(frozen=True)
class TelegramOptions:
    """Raw Telegram flags/non-interactive values; nothing is validated yet."""

    token_stdin: bool = False
    chat_ids: tuple[int, ...] = ()
    user_ids: tuple[int, ...] = ()

    @property
    def requested(self) -> bool:
        return bool(self.token_stdin or self.chat_ids or self.user_ids)


@dataclass(frozen=True)
class TelegramPlan:
    """Validated step-4 inputs for the non-interactive path.

    ``None`` means the corresponding flag was absent, so the stored allowlist
    must be preserved instead of overwritten with an empty list.
    """

    configure: bool = False
    token_stdin: bool = False
    chat_ids: tuple[int, ...] | None = None
    user_ids: tuple[int, ...] | None = None


def read_telegram_token(*, stdin_is_tty: Callable[[], bool]) -> str:
    """Read the bot token from stdin without echo; never from argv."""
    try:
        if stdin_is_tty():
            return getpass.getpass("Telegram bot token (input hidden): ")
        raw = sys.stdin.readline(TELEGRAM_BOT_TOKEN_MAX_LENGTH + 2)
    except (EOFError, OSError):
        raise typer.Abort() from None
    return raw.rstrip("\r\n")


def parse_id_list(raw: str, *, field: str, allow_negative: bool, maximum: int) -> tuple[int, ...]:
    """Parse a comma/space-separated id list through the shared validator.

    Only the integer tokenization is local; sign rules, bounds, de-duplication,
    and sorting all come from :func:`normalize_telegram_ids` so the CLI cannot
    drift from the service.
    """
    text = (raw or "").strip()
    if not text:
        return ()
    values: list[int] = []
    for token in text.replace(" ", ",").split(","):
        item = token.strip()
        if not item:
            continue
        try:
            values.append(int(item, 10))
        except ValueError as exc:
            message = f"{field} must contain only integer ids"
            raise TelegramValidationError(
                message,
                details=[{"field": field, "code": "invalid_id", "message": message}],
            ) from exc
    return normalize_telegram_ids(
        values,
        field=field,
        maximum=maximum,
        allow_negative=allow_negative,
    )


def validate_telegram_ids(chat_ids: tuple[int, ...], user_ids: tuple[int, ...]) -> None:
    """Validate already-parsed flag ids before any write."""
    normalize_telegram_ids(
        chat_ids,
        field="chat_ids",
        maximum=MAX_TELEGRAM_CHAT_IDS,
        allow_negative=True,
    )
    normalize_telegram_ids(
        user_ids,
        field="allowed_user_ids",
        maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
    )


def telegram_label(service: TelegramService) -> str:
    """The only token-shaped text init may print for Telegram."""
    token = service.token_status()
    if token.masked:
        return token.masked
    if token.source == "env":
        return "env"
    return "未配置"


def print_telegram_line(service: TelegramService | None, *, skipped: bool = False) -> None:
    """Print step 4's state with the consequence of leaving pushes disabled."""
    if service is None:
        typer.echo(f"4/5 {_TELEGRAM_STEP_TITLE}: optional; Telegram is unavailable in this build.")
        return
    allowlist = service.allowlist()
    if skipped:
        typer.echo(
            f"4/5 {_TELEGRAM_STEP_TITLE} (optional): 未配置；跳过不会保存任何内容，"
            "Telegram 推送保持禁用。"
        )
        return
    if not service.token_status().configured and not allowlist.chat_ids:
        typer.echo(f"4/5 {_TELEGRAM_STEP_TITLE} (optional): 未配置。")
        return
    typer.echo(
        f"4/5 {_TELEGRAM_STEP_TITLE} (optional): token "
        f"{telegram_label(service)} · "
        f"chats {', '.join(str(item) for item in allowlist.chat_ids) or '-'} · "
        f"users {', '.join(str(item) for item in allowlist.allowed_user_ids) or '-'}"
    )


def maybe_send_test(telegram: TelegramService, prompt_text: PromptText) -> None:
    """The explicit, separate test send; a failure never claims delivery."""
    answer = prompt_text("Send a Telegram test message now? [y/N]", default="")
    if answer.strip().lower() not in ("y", "yes"):
        return
    try:
        record = telegram.enqueue_test(actor="cli")
    except TelegramError as exc:
        typer.echo(f"  test message not sent · {exc.message}", err=True)
        return
    typer.echo(f"  test message {record.state} · {record.event_key}")


def step_telegram_interactive(
    telegram: TelegramService | None,
    service: SettingsService,
    *,
    prompt_text: PromptText,
    read_token: Callable[[], str],
) -> None:
    """Step 4: optional Telegram configuration using the shared services.

    Skipping writes nothing and never completes the step; the printed
    consequence is that all Telegram pushes stay disabled. An already
    configured token (environment or keyring) is reused rather than re-typed.
    """
    print_telegram_line(telegram)
    if telegram is None:  # pragma: no cover - composition always supplies it
        return
    current = telegram.allowlist()
    token = telegram.token_status()
    if token.configured and current.chat_ids:
        maybe_send_test(telegram, prompt_text)
        service.complete_onboarding_step(4, actor="cli")
        return
    answer = prompt_text("Configure Telegram now? [y/N]", default="")
    if answer.strip().lower() not in ("y", "yes"):
        typer.echo("  已跳过：不保存任何内容；Telegram 推送保持禁用。")
        return
    raw_chats = prompt_text(
        "Telegram chat IDs (comma-separated, negative for groups)",
        default=",".join(str(item) for item in current.chat_ids),
    )
    chat_ids = (
        parse_id_list(
            raw_chats,
            field="chat_ids",
            allow_negative=True,
            maximum=MAX_TELEGRAM_CHAT_IDS,
        )
        or current.chat_ids
    )
    raw_users = prompt_text(
        "Telegram allowed user IDs (comma-separated)",
        default=",".join(str(item) for item in current.allowed_user_ids),
    )
    user_ids = (
        parse_id_list(
            raw_users,
            field="allowed_user_ids",
            allow_negative=False,
            maximum=MAX_TELEGRAM_ALLOWED_USER_IDS,
        )
        or current.allowed_user_ids
    )
    if not chat_ids:
        message = "Telegram 至少需要一个 chat id；未保存任何内容。"
        raise TelegramValidationError(
            message,
            details=[{"field": "chat_ids", "code": "chat_required_for_pushes", "message": message}],
        )
    if not token.configured:
        telegram.set_token(read_token(), actor="cli")
        typer.echo(f"  token stored {telegram_label(telegram)}")
    telegram.update_settings(
        chat_ids=list(chat_ids),
        allowed_user_ids=list(user_ids),
        actor="cli",
    )
    maybe_send_test(telegram, prompt_text)
    service.complete_onboarding_step(4, actor="cli")


def plan_telegram(
    *,
    incomplete: tuple[int, ...],
    options: TelegramOptions,
    token_configured: bool,
) -> tuple[TelegramPlan, list[str]]:
    """Decide step 4 for the non-interactive path; nothing is written."""
    if 4 not in incomplete or not options.requested:
        return TelegramPlan(), []
    missing: list[str] = []
    if not token_configured and not options.token_stdin:
        missing.append("--telegram-token-stdin")
    if not options.chat_ids:
        missing.append("--telegram-chat-id")
    validate_telegram_ids(options.chat_ids, options.user_ids)
    return (
        TelegramPlan(
            configure=True,
            token_stdin=options.token_stdin,
            # An absent flag is ``None`` (keep the stored list); an explicit
            # empty tuple is impossible from the CLI option shape.
            chat_ids=options.chat_ids or None,
            user_ids=options.user_ids or None,
        ),
        missing,
    )


def apply_telegram_plan(
    telegram: TelegramService | None,
    plan: TelegramPlan,
    *,
    read_token: Callable[[], str],
) -> None:
    """Apply an already-validated step-4 plan; the service owns the writes."""
    if not plan.configure:
        return
    if telegram is None:  # pragma: no cover - composition always supplies it
        raise ConfigError("Telegram is not available in this build")
    if plan.token_stdin:
        telegram.set_token(read_token(), actor="cli")
    telegram.update_settings(
        chat_ids=list(plan.chat_ids) if plan.chat_ids is not None else None,
        allowed_user_ids=list(plan.user_ids) if plan.user_ids is not None else None,
        actor="cli",
    )
    typer.echo(f"  telegram token {telegram_label(telegram)}")
    # Report the post-apply state: an omitted flag preserved the stored list.
    print_telegram_line(telegram)


__all__ = [
    "TelegramOptions",
    "TelegramPlan",
    "apply_telegram_plan",
    "maybe_send_test",
    "parse_id_list",
    "plan_telegram",
    "print_telegram_line",
    "read_telegram_token",
    "step_telegram_interactive",
    "telegram_label",
    "validate_telegram_ids",
]
