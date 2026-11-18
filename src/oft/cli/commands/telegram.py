"""``oft telegram status|test|receipts|token|allowlist`` (F12).

The token is accepted only on stdin (``getpass`` on a TTY, one line from a
pipe) and every output surface renders the mask only. There is no reveal
command and no write/approval command.
"""

from __future__ import annotations

import getpass
import json
import sys
from typing import Annotated, Any, cast

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.health import probe_daemon
from oft.application.secrets_service import SecretStoreUnavailable
from oft.application.telegram_service import TelegramService, receipt_contract
from oft.application.telegram_watchdog import (
    WatchdogResult,
    resolve_private_chat,
    run_watchdog_once,
)
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings
from oft.domain.settings import SettingValidationError
from oft.ports.telegram import (
    TELEGRAM_CONFLICT,
    TELEGRAM_NOT_CONFIGURED,
    TELEGRAM_TOKEN_INVALID,
    TelegramError,
)

telegram_app = typer.Typer(
    help=(
        "Read-only Telegram channel: status, test send, receipts, token, allowlist. "
        "Authorization requires both an allowlisted chat and an allowlisted user."
    ),
    no_args_is_help=True,
    add_completion=False,
)
token_app = typer.Typer(
    help="Write-only bot token (the value is read from stdin).",
    no_args_is_help=True,
    add_completion=False,
)
allowlist_app = typer.Typer(
    help="Bounded chat/user allowlists.",
    no_args_is_help=True,
    add_completion=False,
)
toggles_app = typer.Typer(
    help="Per-event push toggles (scan report, trigger, entry re-check, position, risk, budget).",
    no_args_is_help=True,
    add_completion=False,
)
telegram_app.add_typer(token_app, name="token")
telegram_app.add_typer(allowlist_app, name="allowlist")
telegram_app.add_typer(toggles_app, name="toggles")


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _container(settings: Any, *, initialize: bool = False) -> Any:
    return build_container(settings, initialize=initialize)


def _config_error_message(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and message else str(exc)


def _exit_config_invalid(exc: Exception, json_output: bool) -> None:
    emit_error("CONFIG_INVALID", _config_error_message(exc), json_output=json_output)
    raise typer.Exit(code=exit_code_for("CONFIG_INVALID"))


def _container_or_exit(settings: Any, *, json_output: bool, initialize: bool = False) -> Any:
    try:
        return _container(settings, initialize=initialize)
    except (ConfigError, SecretStoreUnavailable) as exc:
        _exit_config_invalid(exc, json_output)


def _call_or_exit(call: Any, *, json_output: bool) -> Any:
    """Run one service call, mapping a persisted-config/backend error cleanly."""
    try:
        return call()
    except (ConfigError, SecretStoreUnavailable) as exc:
        _exit_config_invalid(exc, json_output)


def _service(container: Any) -> TelegramService:
    service = cast("TelegramService | None", container.telegram_service)
    if service is None:  # pragma: no cover - composition always supplies it
        emit_error("CONFIG_INVALID", "Telegram is not available", json_output=False)
        raise typer.Exit(code=5)
    return service


def _emit_json(data: dict[str, Any], *, data_quality: str = "COMPLETE") -> None:
    typer.echo(
        json.dumps(
            success_envelope(
                data,
                request_id=new_request_id(),
                provenance="CALC",
                data_quality=data_quality,
            ),
            indent=2,
        )
    )


def _public_code(exc: TelegramError) -> str:
    if exc.code == TELEGRAM_TOKEN_INVALID:
        return TELEGRAM_TOKEN_INVALID
    if exc.code == TELEGRAM_NOT_CONFIGURED:
        return TELEGRAM_NOT_CONFIGURED
    if exc.code == TELEGRAM_CONFLICT:
        return "CONFLICT"
    return "TELEGRAM_DELIVERY_FAILED"


def _emit_telegram_error(exc: TelegramError, json_output: bool) -> int:
    code = _public_code(exc)
    emit_error(code, exc.message, json_output=json_output)
    return exit_code_for(code)


def _read_token_from_stdin() -> str:
    """Read one token without echo; never place it on argv."""
    if sys.stdin.isatty():
        return getpass.getpass("Telegram bot token (input hidden): ")
    return sys.stdin.readline()


_WATCHDOG_FAILED_ACTIONS = frozenset({"ALERT_FAILED", "RECOVERY_FAILED"})


def _watchdog_failure(result: WatchdogResult) -> tuple[str, str] | None:
    """Map a watchdog outcome to ``(error_code, bounded message)``, or ``None``.

    A failed send or an unreadable persisted state is a real failure for cron
    and systemd ``OnFailure=``; only ``NONE``/``ALERT_SENT``/``RECOVERY_SENT``
    are success. The message is built from the result only, never a secret.
    """
    if result.action in _WATCHDOG_FAILED_ACTIONS:
        code = "TELEGRAM_DELIVERY_FAILED"
        message = f"watchdog {result.daemon_state}: {result.action}"
    elif result.daemon_state == "UNKNOWN":
        code = "DATA_UNAVAILABLE"
        message = "watchdog daemon state is UNKNOWN"
    else:
        return None
    if result.detail:
        message = f"{message}: {result.detail}"
    return code, message


@telegram_app.command("status")
def status_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Report NOT_CONFIGURED / NO_ALLOWLIST / READY / DEGRADED (no network)."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output))
    status = _call_or_exit(service.status_state, json_output=json_output)
    if json_output:
        _emit_json(
            status.to_contract(),
            data_quality="COMPLETE" if status.state != "DEGRADED" else "PARTIAL",
        )
        return
    token = _call_or_exit(service.token_status, json_output=json_output)
    masked = token.masked or ("env" if token.source == "env" else "unset")
    typer.echo(
        "\n".join(
            [
                f"state      {status.state}",
                f"token      {masked}",
                f"chats      {status.chat_count}",
                f"users      {status.allowed_user_count}",
                f"detail     {status.detail}",
            ]
        )
    )


@telegram_app.command("watchdog")
def watchdog_command(
    once: Annotated[
        bool, typer.Option("--once", help="Run one probe and exit (the only mode).")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """One-shot host check for a dead daemon (for cron/systemd ``OnFailure=``).

    This is not a long-running process and is not started by ``daemon start``.
    It sends at most one alert and one recovery per outage and never prints the
    bot token.
    """
    if not once:
        emit_error(
            "VALIDATION_FAILED",
            "watchdog is one-shot only; pass --once",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(json_output)
    # The watchdog persists its outage epoch, so it must own an initialized
    # store even when it is the first command to touch the data directory.
    container = _container_or_exit(settings, json_output=json_output, initialize=True)
    service = _service(container)
    allowlist = _call_or_exit(service.allowlist, json_output=json_output)
    result = run_watchdog_once(
        store=container.store,
        clock=container.clock,
        probe=lambda: probe_daemon(settings),
        port=container.telegram_port,
        chat_id=resolve_private_chat(allowlist.chat_ids),
        lock_path=settings.data_dir / "telegram-watchdog.lock",
    )
    failure = _watchdog_failure(result)
    if json_output:
        if failure is not None:
            code, message = failure
            emit_error(code, message, json_output=True)
            raise typer.Exit(code=exit_code_for(code))
        _emit_json(result.to_contract())
        return
    typer.echo(f"daemon {result.daemon_state} · {result.action}")
    if result.detail:
        typer.echo(f"detail {result.detail}")
    if failure is not None:
        code, message = failure
        emit_error(code, message, json_output=False)
        raise typer.Exit(code=exit_code_for(code))


@telegram_app.command("test")
def test_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Probe the token and enqueue one test message; return its receipt."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output, initialize=True))
    try:
        record = _call_or_exit(lambda: service.enqueue_test(actor="cli"), json_output=json_output)
    except TelegramError as exc:
        raise typer.Exit(code=_emit_telegram_error(exc, json_output)) from exc
    payload = receipt_contract(record)
    if json_output:
        _emit_json(payload)
        return
    typer.echo(f"test message enqueued · {record.event_key} · {record.state}")


@telegram_app.command("receipts")
def receipts_command(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100, help="Rows.")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show bounded Telegram outbox receipts, newest first."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output))
    records = _call_or_exit(lambda: service.receipts(limit=limit), json_output=json_output)
    total = _call_or_exit(service.receipt_count, json_output=json_output)
    if json_output:
        _emit_json(
            {
                "items": [receipt_contract(record) for record in records],
                "total": total,
            },
            data_quality="COMPLETE" if records else "MISSING",
        )
        return
    if not records:
        typer.echo("no Telegram receipts")
        return
    lines = [f"{'EVENT':<32}{'KIND':<14}{'STATE':<10}{'ATTEMPTS':<10}MESSAGE"]
    for record in records:
        lines.append(
            f"{record.event_key:<32}{record.kind:<14}{record.state:<10}"
            f"{record.attempts:<10}{record.message_id if record.message_id is not None else '-'}"
        )
    typer.echo("\n".join(lines))


@token_app.command("set")
def token_set_command(
    read_stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the token from stdin (the only source).")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Store or rotate the bot token from stdin; output is the mask only."""
    if not read_stdin:
        emit_error(
            "VALIDATION_FAILED",
            "a bot token is accepted only on stdin; pass --stdin and never argv",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    token = _read_token_from_stdin()
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output, initialize=True))
    try:
        status = service.set_token(token, actor="cli")
    except SettingValidationError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    except (ConfigError, SecretStoreUnavailable) as exc:
        _exit_config_invalid(exc, json_output)
    payload = status.to_contract()
    if json_output:
        _emit_json(payload)
        return
    typer.echo(f"telegram token stored · {status.masked}")


@token_app.command("delete")
def token_delete_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Delete the stored token; an environment token, if set, still applies."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output, initialize=True))
    try:
        status = service.delete_token(actor="cli")
    except SettingValidationError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    except (ConfigError, SecretStoreUnavailable) as exc:
        _exit_config_invalid(exc, json_output)
    payload = status.to_contract()
    if json_output:
        _emit_json(payload)
        return
    typer.echo(f"telegram token deleted · {'configured' if status.configured else 'unset'}")


def _allowlist_contract(service: TelegramService) -> dict[str, Any]:
    return service.allowlist().to_contract()


@allowlist_app.command("show")
def allowlist_show_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show both bounded allowlists and their last change."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output))
    payload = _call_or_exit(lambda: _allowlist_contract(service), json_output=json_output)
    if json_output:
        _emit_json(payload)
        return
    typer.echo(
        "\n".join(
            [
                f"chat_ids          {', '.join(str(item) for item in payload['chat_ids']) or '-'}",
                "allowed_user_ids  "
                f"{', '.join(str(item) for item in payload['allowed_user_ids']) or '-'}",
                f"updated_at        {payload['updated_at'] or '-'}",
            ]
        )
    )


def _update_allowlist(
    *,
    add: bool,
    chat_ids: list[int],
    user_ids: list[int],
    json_output: bool,
) -> None:
    if not chat_ids and not user_ids:
        emit_error(
            "VALIDATION_FAILED",
            "provide at least one --chat-id or --user-id selector",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output, initialize=True))
    current = _call_or_exit(service.allowlist, json_output=json_output)
    current_chats = set(current.chat_ids)
    current_users = set(current.allowed_user_ids)
    if add:
        next_chats = current_chats | set(chat_ids)
        next_users = current_users | set(user_ids)
    else:
        next_chats = current_chats - set(chat_ids)
        next_users = current_users - set(user_ids)
    try:
        result = _call_or_exit(
            lambda: service.update_allowlist(sorted(next_chats), sorted(next_users), actor="cli"),
            json_output=json_output,
        )
    except SettingValidationError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=json_output)
        raise typer.Exit(code=2) from exc
    payload = result.to_contract()
    if json_output:
        _emit_json(payload)
        return
    action = "added" if add else "removed"
    typer.echo(
        f"telegram allowlist {action} · chats {payload['chat_ids']} · "
        f"users {payload['allowed_user_ids']}"
    )


@allowlist_app.command("add")
def allowlist_add_command(
    chat_id: Annotated[
        list[int] | None,
        typer.Option("--chat-id", help="Chat id to allow (repeatable)."),
    ] = None,
    user_id: Annotated[
        list[int] | None,
        typer.Option("--user-id", help="User id to allow (repeatable)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Add one or more chat/user ids to the allowlist."""
    _update_allowlist(
        add=True,
        chat_ids=list(chat_id or []),
        user_ids=list(user_id or []),
        json_output=json_output,
    )


@allowlist_app.command("remove")
def allowlist_remove_command(
    chat_id: Annotated[
        list[int] | None,
        typer.Option("--chat-id", help="Chat id to remove (repeatable)."),
    ] = None,
    user_id: Annotated[
        list[int] | None,
        typer.Option("--user-id", help="User id to remove (repeatable)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Remove one or more chat/user ids from the allowlist."""
    _update_allowlist(
        add=False,
        chat_ids=list(chat_id or []),
        user_ids=list(user_id or []),
        json_output=json_output,
    )


@toggles_app.command("show")
def toggles_show_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the six push toggles and their provenance."""
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output))
    state = _call_or_exit(service.settings_state, json_output=json_output)
    payload = state.to_contract()
    if json_output:
        _emit_json(payload)
        return
    lines = [
        f"{name:<16}{'on' if enabled else 'off'}" for name, enabled in payload["toggles"].items()
    ]
    lines.append(f"source          {payload['source']}")
    lines.append(f"updated_at      {payload['updated_at'] or '-'}")
    typer.echo("\n".join(lines))


@toggles_app.command("set")
def toggles_set_command(
    scan_report: Annotated[
        bool | None,
        typer.Option("--scan-report/--no-scan-report", help="Scan-completion summary."),
    ] = None,
    trigger: Annotated[
        bool | None,
        typer.Option("--trigger/--no-trigger", help="Opportunity triggered/invalidated alerts."),
    ] = None,
    entry_recheck: Annotated[
        bool | None,
        typer.Option(
            "--entry-recheck/--no-entry-recheck", help="Entry re-check awaiting-human alerts."
        ),
    ] = None,
    position: Annotated[
        bool | None,
        typer.Option("--position/--no-position", help="Position management executed/blocked."),
    ] = None,
    risk: Annotated[
        bool | None,
        typer.Option("--risk/--no-risk", help="Risk and degradation alerts."),
    ] = None,
    budget: Annotated[
        bool | None,
        typer.Option("--budget/--no-budget", help="Budget threshold alerts."),
    ] = None,
    chat_id: Annotated[
        list[int] | None,
        typer.Option("--chat-id", help="Replace the chat allowlist (repeatable)."),
    ] = None,
    user_id: Annotated[
        list[int] | None,
        typer.Option("--user-id", help="Replace the user allowlist (repeatable)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Update one or more toggles (and optionally the allowlists) atomically.

    At least one chat id is required while any toggle is enabled; an all-off
    state with no chats is allowed.
    """
    toggles = {
        name: value
        for name, value in (
            ("scan_report", scan_report),
            ("trigger", trigger),
            ("entry_recheck", entry_recheck),
            ("position", position),
            ("risk", risk),
            ("budget", budget),
        )
        if value is not None
    }
    if not toggles and chat_id is None and user_id is None:
        emit_error(
            "VALIDATION_FAILED",
            "provide at least one toggle or allowlist selector",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(json_output)
    service = _service(_container_or_exit(settings, json_output=json_output, initialize=True))
    try:
        state = service.update_settings(
            chat_ids=None if chat_id is None else list(chat_id),
            allowed_user_ids=None if user_id is None else list(user_id),
            toggles=toggles or None,
            actor="cli",
        )
    except SettingValidationError as exc:
        emit_error("VALIDATION_FAILED", exc.message, json_output=json_output, details=exc.details)
        raise typer.Exit(code=2) from exc
    except (ConfigError, SecretStoreUnavailable) as exc:
        _exit_config_invalid(exc, json_output)
    payload = state.to_contract()
    if json_output:
        _emit_json(payload)
        return
    typer.echo(
        "telegram toggles updated · "
        + ", ".join(
            f"{name}={'on' if enabled else 'off'}" for name, enabled in payload["toggles"].items()
        )
    )


__all__ = ["allowlist_app", "telegram_app", "toggles_app", "token_app"]
