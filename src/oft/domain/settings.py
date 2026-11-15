"""The one persisted runtime setting: ``scan.period`` (slice 32).

Only ``4h`` and ``8h`` are supported (G3 decision 1). The domain module is the
single source for the key, the supported values, the validation error shape,
and the source enum; the scheduler aliases these values rather than restating
them.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

SCAN_PERIOD_KEY = "scan.period"
# One source of truth for every scan cadence: the supported label and its hour
# length. ``SUPPORTED_SCAN_PERIODS`` and the scheduler's period/boundary maps are
# derived from it, so adding a cadence is a one-line domain change.
SCAN_PERIOD_HOURS: Mapping[str, int] = {"4h": 4, "8h": 8}
SUPPORTED_SCAN_PERIODS: tuple[str, ...] = tuple(SCAN_PERIOD_HOURS)

# One audit event type and subject kind are enough for the config path; other
# settings slices reuse ``CONFIG_UPDATED`` with their own key.
CONFIG_UPDATED_EVENT = "CONFIG_UPDATED"
CONFIG_SUBJECT_KIND = "CONFIG"

VALIDATION_FAILED = "VALIDATION_FAILED"


class SettingSource(StrEnum):
    """Where an effective value came from; never guessed."""

    DEFAULT = "default"
    USER = "user"


class SettingValidationError(Exception):
    """A setting value failed typed validation; surfaced as ``VALIDATION_FAILED``."""

    code = VALIDATION_FAILED

    def __init__(
        self,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: list[dict[str, Any]] = details or []


def _period_detail(code: str, message: str) -> dict[str, Any]:
    return {"field": "period", "code": code, "message": message}


def validate_scan_period(value: object) -> str:
    """Return the normalized period or raise a field-scoped validation error."""
    if not isinstance(value, str):
        raise SettingValidationError(
            "scan.period must be a string",
            details=[_period_detail("not_a_string", "scan.period must be a string")],
        )
    normalized = value.strip()
    if not normalized:
        raise SettingValidationError(
            "scan.period must not be empty",
            details=[_period_detail("empty", "scan.period must not be empty")],
        )
    if normalized not in SUPPORTED_SCAN_PERIODS:
        expected = ", ".join(SUPPORTED_SCAN_PERIODS)
        message = f"scan.period must be one of: {expected}"
        raise SettingValidationError(
            message,
            details=[_period_detail("unsupported", message)],
        )
    return normalized


def scoped_diff(*, key: str, before: Any, after: Any) -> dict[str, Any]:
    """The scoped before/after diff recorded in the audit payload."""
    return {"key": key, "before": before, "after": after}


__all__ = [
    "CONFIG_SUBJECT_KIND",
    "CONFIG_UPDATED_EVENT",
    "SCAN_PERIOD_HOURS",
    "SCAN_PERIOD_KEY",
    "SUPPORTED_SCAN_PERIODS",
    "SettingSource",
    "SettingValidationError",
    "VALIDATION_FAILED",
    "scoped_diff",
    "validate_scan_period",
]
