"""Shared error envelope schemas and OpenAPI response builders.

Every route keeps its own error catalog, but the envelope shape and the
``responses=`` mapping are identical, so they live here instead of being copied
per schema module (M12).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


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


def catalog_responses(
    catalog: Mapping[int, tuple[str, str]],
) -> dict[int | str, dict[str, Any]]:
    """Build FastAPI ``responses`` entries from a ``{status: (code, description)}`` map."""
    return {
        status: {"model": ErrorEnvelope, "description": description}
        for status, (_code, description) in catalog.items()
    }


__all__ = ["ErrorBody", "ErrorEnvelope", "catalog_responses"]
