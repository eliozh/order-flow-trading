"""Settings request and response schemas (slices 32/34)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oft.api.schemas.envelope import Envelope
from oft.api.schemas.errors import ErrorBody, ErrorEnvelope, catalog_responses
from oft.domain.onboarding import OnboardingStepId
from oft.domain.prompts import PromptRole


class ScanSettingsUpdate(BaseModel):
    """The one supported scan setting; extra fields are rejected."""

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


RiskSource = Literal["paper_account", "none"]
RiskQuality = Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]


class RiskUtilizationMetricData(BaseModel):
    used: float | None
    cap: float
    unit: str
    source: RiskSource
    quality: RiskQuality
    note: str | None = None


class RiskUtilizationData(BaseModel):
    source: RiskSource
    quality: RiskQuality
    stale_inputs: list[str]
    metrics: dict[str, RiskUtilizationMetricData]


class RiskSettingsUpdate(BaseModel):
    """A partial cap patch; ``confirm`` authorizes loosening.

    The cap keys/values are validated by the shared service (which reuses the
    domain's typed parser), so a bool, NaN, unknown field, or range violation
    produces the same field-scoped details the CLI reports.
    """

    model_config = ConfigDict(extra="forbid")

    caps: dict[str, Any]
    confirm: bool = False
    reason: str = Field(default="", max_length=500)


class RiskSettingsData(BaseModel):
    caps: dict[str, float | int]
    source: Literal["default", "user"]
    defaults: dict[str, float | int]
    utilization: RiskUtilizationData
    updated_at: str | None = None
    updated_by: str | None = None


class LLMRolesData(BaseModel):
    """The four role models; the active provider applies to every role."""

    ANALYSIS: str | None = None
    ENTRY_RECHECK: str | None = None
    MANAGEMENT: str | None = None
    REVIEW: str | None = None


class LLMConcurrencyData(BaseModel):
    global_: int = Field(alias="global")
    deepseek: int

    model_config = ConfigDict(populate_by_name=True)


class LLMCapabilityData(BaseModel):
    status: Literal["STRICT", "DEGRADED", "FAILED", "UNCHECKED"]
    strict_schema: bool
    degraded: bool
    checked_at: str
    notes: str


class LLMProviderRowData(BaseModel):
    """One provider row; ``masked`` is the only key-shaped value."""

    provider: Literal["openai", "anthropic", "deepseek"]
    configured: bool
    key_source: Literal["env", "keyring", "encrypted_file", "stored", "unset", "unavailable"]
    masked: str | None = None
    fingerprint: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    base_url: str
    capability: LLMCapabilityData | None = None


class LLMSettingsData(BaseModel):
    provider: Literal["openai", "anthropic", "deepseek"] | None = None
    providers: list[LLMProviderRowData]
    roles: LLMRolesData
    base_urls: dict[str, str]
    allow_custom_base_url: bool
    concurrency: LLMConcurrencyData
    source: Literal["default", "user"]
    updated_at: str | None = None
    updated_by: str | None = None


class LLMSettingsUpdate(BaseModel):
    """A partial LLM settings patch; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    roles: dict[str, str | None] | None = None
    base_urls: dict[str, str] | None = None
    allow_custom_base_url: bool | None = None
    max_concurrency: int | None = None
    max_concurrency_deepseek: int | None = None
    reason: str = Field(default="", max_length=500)


class LLMKeyUpdate(BaseModel):
    """The write-only key payload; it is never echoed in a response."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=512)


class LLMKeyDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMKeyStatusData(BaseModel):
    provider: Literal["openai", "anthropic", "deepseek"]
    configured: bool
    key_source: Literal["env", "keyring", "encrypted_file", "stored", "unset", "unavailable"]
    masked: str | None = None
    fingerprint: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


# -- slice 36: prompt templates (F10b), list-only read aggregate -------------


class PromptSettingsRowData(BaseModel):
    """One USER prompt role, metadata only (no body, hash, or secret)."""

    prompt_id: str
    role: PromptRole
    active_version: int
    author: str
    updated_at: str
    version_count: int
    latest_version: int
    required_variables: list[str]
    is_builtin_default: bool


class PromptSettingsData(BaseModel):
    items: list[PromptSettingsRowData]
    total: int


# -- slice 44: paper-only onboarding progress --------------------------------


class OnboardingProgressUpdate(BaseModel):
    """One step completion; the service still rejects deferred step 3."""

    model_config = ConfigDict(extra="forbid")

    step: int = Field(strict=True, ge=1, le=5)


class OnboardingStepData(BaseModel):
    """One step's state; the deferred/milestone pair must stay consistent."""

    step: int = Field(ge=1, le=5)
    id: OnboardingStepId
    title: str
    deferred: bool
    deferred_milestone: int | None = None
    optional: bool = False
    completed: bool

    @model_validator(mode="after")
    def _check_deferred_invariant(self) -> OnboardingStepData:
        if self.deferred != (self.deferred_milestone is not None):
            raise ValueError("deferred must match deferred_milestone presence")
        if self.deferred_milestone is not None and self.deferred_milestone < 1:
            raise ValueError("deferred_milestone must be a positive integer")
        return self


class OnboardingProgressData(BaseModel):
    """Completed steps and the resume point; never a secret or a key value."""

    steps: list[OnboardingStepData]
    completed_steps: list[int]
    resumed_step: int = Field(ge=1, le=5)
    all_complete: bool
    environment: Literal["paper"]
    updated_at: str | None = None
    updated_by: str | None = None


_CATALOG_ERRORS = {
    400: ("IDEMPOTENCY_KEY_REQUIRED", "A state-changing settings request omitted Idempotency-Key."),
    401: ("AUTH_REQUIRED", "The local API credential is missing or invalid."),
    403: ("CSRF_INVALID", "The CSRF token or Origin check failed."),
    409: ("IDEMPOTENCY_CONFLICT", "The same key was used with a different payload."),
    422: ("VALIDATION_FAILED", "The setting failed typed validation."),
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
    "LLMCapabilityData",
    "LLMConcurrencyData",
    "LLMKeyDelete",
    "LLMKeyStatusData",
    "LLMKeyUpdate",
    "LLMProviderRowData",
    "LLMRolesData",
    "LLMSettingsData",
    "LLMSettingsUpdate",
    "OnboardingProgressData",
    "OnboardingProgressUpdate",
    "OnboardingStepData",
    "OnboardingStepId",
    "PromptSettingsData",
    "PromptSettingsRowData",
    "RiskSettingsData",
    "RiskSettingsUpdate",
    "RiskUtilizationData",
    "RiskUtilizationMetricData",
    "ScanSettingsData",
    "ScanSettingsUpdate",
    "SettingsSectionData",
    "SettingsSectionsData",
    "mutation_openapi",
]
