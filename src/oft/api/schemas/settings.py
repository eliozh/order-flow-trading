"""Settings request and response schemas (slice 32: ``scan.period`` only)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oft.api.schemas.envelope import Envelope


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


class ErrorBody(BaseModel):
    code: str
    message: str
    category: str
    http_status: int
    retryable: bool
    reference_id: str
    details: list[dict[str, Any]] | None = None


class ErrorEnvelope(BaseModel):
    ok: bool
    data: None
    error: ErrorBody
    meta: dict[str, Any]


_CATALOG_ERRORS = {
    400: ("IDEMPOTENCY_KEY_REQUIRED", "A state-changing settings request omitted Idempotency-Key."),
    401: ("AUTH_REQUIRED", "The local API credential is missing or invalid."),
    403: ("CSRF_INVALID", "The CSRF token or Origin check failed."),
    409: ("IDEMPOTENCY_CONFLICT", "The same key was used with a different payload."),
    422: ("VALIDATION_FAILED", "The scan period failed typed validation."),
    500: ("CONFIG_INVALID", "The configuration is missing, unreadable, or inconsistent."),
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope, "description": description}
    for status, (_code, description) in _CATALOG_ERRORS.items()
}


def mutation_openapi() -> dict[str, Any]:
    """Document only the mandatory ``Idempotency-Key`` header for settings PUTs."""
    return {
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": "Mandatory; a replay with a different payload is a 409.",
                "schema": {"type": "string", "maxLength": 200},
            }
        ]
    }


__all__ = [
    "ERROR_RESPONSES",
    "Envelope",
    "ErrorEnvelope",
    "ScanSettingsData",
    "ScanSettingsUpdate",
    "SettingsSectionData",
    "SettingsSectionsData",
    "mutation_openapi",
]
