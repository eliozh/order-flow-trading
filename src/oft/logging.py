"""Structured JSON logging with credential redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from oft.config import Settings

REDACTED = "[REDACTED]"
_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "authorization")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER_PATTERN.sub(f"Bearer {REDACTED}", value)
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


def configure_logging(settings: Settings) -> None:
    """Configure structlog JSON output to stdout and the daemon log file."""
    level = getattr(logging, settings.log_level)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(log_dir / "daemon.log", encoding="utf-8"))
    logging.basicConfig(format="%(message)s", handlers=handlers, level=level, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
