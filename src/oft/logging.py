"""Structured JSON logging with credential redaction.

Two independent pipelines are scrubbed:

- the ``structlog`` processor :func:`redact_secrets` (JSON event dicts), and
- a standard-library :class:`logging.Filter` installed on every root handler,
  because third-party loggers (``httpx``/``httpcore``) write stdlib records
  directly. The Bot API token is embedded in the request path, and ``httpx``
  logs the full URL at INFO, so the stdlib filter is mandatory.

The filter applies the Telegram token-shape regex, the bearer-token regex, and
an exact-value registry populated from ``SecretsService.redaction_values()`` so
an environment token outside the canonical shape is still scrubbed. The value
registry never exposes the values; it only replaces them with ``[REDACTED]``.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

import structlog

from oft.config import Settings
from oft.domain.telegram import TELEGRAM_BOT_TOKEN_PATTERN

REDACTED = "[REDACTED]"
_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "authorization")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# Values shorter than this are not treated as exact redaction values: replacing
# a short common string everywhere would corrupt unrelated log text.
_MIN_REDACTION_VALUE_LENGTH = 8

_REDACTION_VALUES_LOCK = threading.Lock()
_REDACTION_VALUES: frozenset[str] = frozenset()


def set_redaction_values(values: Iterable[str]) -> None:
    """Register in-process secret values the logging pipeline must never emit.

    Called after the secret/Telegram service is built. A store failure at the
    call site must not block startup; this function itself only stores the
    values it is given and never touches a backend.
    """
    global _REDACTION_VALUES
    cleaned = {
        value
        for value in values
        if isinstance(value, str) and len(value) >= _MIN_REDACTION_VALUE_LENGTH
    }
    with _REDACTION_VALUES_LOCK:
        _REDACTION_VALUES = frozenset(cleaned)


def _registered_values() -> frozenset[str]:
    with _REDACTION_VALUES_LOCK:
        return _REDACTION_VALUES


def _scrub_text(value: str) -> str:
    """Mask bearer tokens, bot tokens, and every registered exact secret."""
    scrubbed = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", value)
    for secret in sorted(_registered_values(), key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, REDACTED)
    return TELEGRAM_BOT_TOKEN_PATTERN.sub(REDACTED, scrubbed)


def _redact_value(value: Any) -> Any:
    """Recursively scrub string values, including nested dict/list payloads."""
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if item not in (None, "")
                and any(hint in str(key).lower() for hint in _SENSITIVE_HINTS)
                else _redact_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_secrets(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Return a copy of the event with credentials and bearer tokens masked."""
    return {
        key: (
            REDACTED
            if value not in (None, "") and any(hint in key.lower() for hint in _SENSITIVE_HINTS)
            else _redact_value(value)
        )
        for key, value in event_dict.items()
    }


class RedactingFilter(logging.Filter):
    """Scrub one stdlib log record before either root handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging on a bad record
            return True
        scrubbed = _scrub_text(rendered)
        if scrubbed != rendered:
            # Replace the already-formatted record so the bound args cannot
            # re-introduce the secret (and ``%`` characters stay literal).
            record.msg = scrubbed
            record.args = ()
        if record.exc_info is not None:
            try:
                # The stdlib formatter appends ``exc_text`` after handler
                # filters run, so scrub it here (and mark it rendered).
                record.exc_text = _scrub_text(logging.Formatter().formatException(record.exc_info))
            except Exception:  # noqa: BLE001 - never break logging on a bad record
                record.exc_text = None
        if record.stack_info:
            with contextlib.suppress(Exception):
                record.stack_info = _scrub_text(record.stack_info)
        return True


class _ScrubbingFormatter(logging.Formatter):
    """Scrub the fully rendered record (message + traceback + stack info).

    Defense in depth for records that bypass the filter (a handler added after
    configuration, or a formatter that renders ``exc_text`` itself).
    """

    def format(self, record: logging.LogRecord) -> str:
        return _scrub_text(super().format(record))


def configure_logging(settings: Settings) -> None:
    """Configure structlog JSON output to stdout and the daemon log file."""
    level = getattr(logging, settings.log_level)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactingFilter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(log_dir / "daemon.log", encoding="utf-8"))
    for handler in handlers:
        handler.addFilter(redactor)
    logging.basicConfig(format="%(message)s", handlers=handlers, level=level, force=True)
    # ``basicConfig`` may install the plain formatter; replace it on the root
    # handlers with one that scrubs the whole rendered string, including the
    # traceback the stdlib appends after the filter runs.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_ScrubbingFormatter("%(message)s"))
    # ``httpx`` logs every request URL (which carries the bot token) at INFO.
    # Keep only warnings and above; the handler-level filter scrubs those too.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Render the exception into the event dict *before* redaction, so
            # an ``exc_info`` payload is scrubbed like any other value.
            structlog.processors.format_exc_info,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


__all__ = [
    "REDACTED",
    "RedactingFilter",
    "configure_logging",
    "redact_secrets",
    "set_redaction_values",
]
