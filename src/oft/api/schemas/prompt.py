"""Prompt catalog request and response schemas (ADR-0009)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oft.api.schemas.trigger import Envelope
from oft.domain.prompts import PromptKind, PromptRole, PromptStatus

MAX_PROMPT_BODY_BYTES = 64 * 1024
MAX_HASH_LENGTH = 128


class EditPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_PROMPT_BODY_BYTES)
    author: str = Field(default="cli", max_length=64)
    reason: str = Field(default="", max_length=500)
    activate: bool = False
    knowledge_artifact_hash: str | None = Field(default=None, max_length=MAX_HASH_LENGTH)

    @field_validator("body")
    @classmethod
    def _body_fits_utf8_bytes(cls, value: str) -> str:
        # ``max_length`` counts code points; the persisted/LLM budget is bytes.
        if len(value.encode("utf-8")) > MAX_PROMPT_BODY_BYTES:
            raise ValueError(f"body must be at most {MAX_PROMPT_BODY_BYTES} UTF-8 bytes")
        return value

    @field_validator("knowledge_artifact_hash")
    @classmethod
    def _hash_fits_utf8_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > MAX_HASH_LENGTH:
            raise ValueError(f"hash must be at most {MAX_HASH_LENGTH} UTF-8 bytes")
        return value


class ActivatePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    actor: str = Field(default="cli", max_length=64)
    reason: str = Field(default="", max_length=500)


class RestorePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str = Field(default="cli", max_length=64)
    reason: str = Field(default="restore default", max_length=500)


class PromptVersionData(BaseModel):
    prompt_id: str
    version: int
    status: PromptStatus
    is_active: bool
    body: str
    body_hash: str
    required_variables: list[str]
    author: str
    reason: str
    is_default: bool
    created_at: str
    system_prompt_id: str
    system_prompt_version: int
    system_prompt_hash: str
    knowledge_artifact_hash: str | None


class PromptSummaryData(BaseModel):
    prompt_id: str
    role: PromptRole
    kind: PromptKind
    active_version: int
    version_count: int
    latest_version: int
    updated_at: str


class PromptListData(BaseModel):
    items: list[PromptSummaryData]
    total: int


class PromptDetailData(BaseModel):
    prompt_id: str
    role: PromptRole
    kind: PromptKind
    active_version: int
    created_at: str
    updated_at: str
    versions: list[PromptVersionData]


class PromptHistoryData(BaseModel):
    prompt_id: str
    items: list[PromptVersionData]
    total: int


class PromptDiffData(BaseModel):
    prompt_id: str
    from_version: int
    to_version: int
    diff: str


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
    400: ("IDEMPOTENCY_KEY_REQUIRED", "A state-changing request omitted Idempotency-Key."),
    401: ("AUTH_REQUIRED", "The local API credential is missing or invalid."),
    403: ("CSRF_INVALID", "The CSRF token or Origin check failed."),
    404: ("NOT_FOUND", "The prompt template does not exist."),
    409: (
        "PROMPT_VERSION_MISSING",
        "The prompt version is missing or not active (PROMPT_VERSION_MISSING), or a "
        "concurrent edit took the next version (CONFLICT).",
    ),
    422: ("PROMPT_VALIDATION_FAILED", "The template body failed validation."),
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope, "description": description}
    for status, (_code, description) in _CATALOG_ERRORS.items()
}


def mutation_openapi() -> dict[str, Any]:
    """Document the mandatory Idempotency-Key header for mutation routes.

    ``openapi_extra`` replaces the generated ``parameters`` list, so the path
    parameter is restated here.
    """
    return {
        "parameters": [
            {
                "name": "prompt_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": "Mandatory; a replay with a different payload is a 409.",
                "schema": {"type": "string", "maxLength": 200},
            },
        ]
    }


__all__ = [
    "ActivatePromptRequest",
    "EditPromptRequest",
    "ERROR_RESPONSES",
    "Envelope",
    "ErrorEnvelope",
    "PromptDetailData",
    "PromptDiffData",
    "PromptHistoryData",
    "PromptListData",
    "PromptSummaryData",
    "PromptVersionData",
    "RestorePromptRequest",
    "mutation_openapi",
]
