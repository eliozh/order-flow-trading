"""Settings request and response schemas (slice 32: ``scan.period`` only)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oft.api.schemas.envelope import Envelope
from oft.api.schemas.errors import ErrorBody, ErrorEnvelope, catalog_responses


class ScanSettingsUpdate(BaseModel):
    """The one supported setting in this slice; extra fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    period: str


class ScanSettingsData(BaseModel):
    key: str
    value: str
    source: Literal["default", "user"]
    supported: list[str]
    next_run: str = Field(
        description=(
            "Next UTC boundary for the effective period. A saved change applies "
            "at the next boundary of the schedule active when it was saved, so "
            "the daemon may apply it later than this field while pending."
        )
    )
    timezone: str
    updated_at: str | None = None
    updated_by: str | None = None


class SettingsSectionData(BaseModel):
    section: str
    source: Literal["default", "user"]
    last_changed: str | None = None
    keys: list[str]


class SettingsSectionsData(BaseModel):
    sections: list[SettingsSectionData]


_CATALOG_ERRORS = {
    400: ("IDEMPOTENCY_KEY_REQUIRED", "A state-changing settings request omitted Idempotency-Key."),
    401: ("AUTH_REQUIRED", "The local API credential is missing or invalid."),
    403: ("CSRF_INVALID", "The CSRF token or Origin check failed."),
    409: ("IDEMPOTENCY_CONFLICT", "The same key was used with a different payload."),
    422: ("VALIDATION_FAILED", "The scan period failed typed validation."),
    500: ("CONFIG_INVALID", "The configuration is missing, unreadable, or inconsistent."),
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = catalog_responses(_CATALOG_ERRORS)


def mutation_openapi() -> dict[str, Any]:
    """Document the mandatory ``Idempotency-Key`` header for settings PUTs.

    A save that changes the value writes one ``config`` row and one
    ``CONFIG_UPDATED`` audit event. A save that repeats the already-effective
    value returns 200 with no new row and no audit event. An identical replay
    (same key, same payload) returns the stored envelope with
    ``meta.idempotent_replay=true`` and writes nothing.
    """
    return {
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": (
                    "Mandatory; a replay with a different payload is a 409. "
                    "An identical replay returns the stored envelope "
                    "(meta.idempotent_replay=true) and writes nothing."
                ),
                "schema": {"type": "string", "maxLength": 200},
            }
        ]
    }


__all__ = [
    "ERROR_RESPONSES",
    "Envelope",
    "ErrorBody",
    "ErrorEnvelope",
    "ScanSettingsData",
    "ScanSettingsUpdate",
    "SettingsSectionData",
    "SettingsSectionsData",
    "mutation_openapi",
]
