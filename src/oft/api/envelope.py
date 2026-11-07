"""The single API response envelope (docs/contracts/api-envelope.schema.json)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from oft.domain.time import utc_timestamp

_REQUEST_ID_ALPHABET_LENGTH = 12


def new_request_id() -> str:
    return f"REQ-{uuid.uuid4().hex[:_REQUEST_ID_ALPHABET_LENGTH].upper()}"


def _meta(
    *,
    request_id: str,
    generated_at: str,
    duration_ms: int,
    provenance: str,
    data_quality: str | None,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "generated_at": generated_at,
        "duration_ms": duration_ms,
        "provenance": provenance,
        "data_quality": data_quality,
        "warnings": [],
        "pagination": None,
        "idempotent_replay": idempotent_replay,
    }


def success_envelope(
    data: Any,
    *,
    request_id: str,
    generated_at: str | None = None,
    duration_ms: int = 0,
    provenance: str = "CALC",
    data_quality: str | None = "COMPLETE",
    pagination: dict[str, Any] | None = None,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    """Build an ``ok: true`` envelope."""
    timestamp = generated_at or utc_timestamp(datetime.now(UTC))
    meta = _meta(
        request_id=request_id,
        generated_at=timestamp,
        duration_ms=duration_ms,
        provenance=provenance,
        data_quality=data_quality,
        idempotent_replay=idempotent_replay,
    )
    meta["pagination"] = pagination
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": meta,
    }


def error_envelope(
    *,
    code: str,
    message: str,
    http_status: int,
    category: str,
    retryable: bool,
    reference_id: str,
    request_id: str | None = None,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an ``ok: false`` envelope with a catalog error."""
    resolved_request_id = request_id or new_request_id()
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "category": category,
            "http_status": http_status,
            "retryable": retryable,
            "reference_id": reference_id,
            "details": details or [],
        },
        "meta": _meta(
            request_id=resolved_request_id,
            generated_at=utc_timestamp(datetime.now(UTC)),
            duration_ms=0,
            provenance="NONE",
            data_quality=None,
        ),
    }
