"""The single typed API success envelope (docs/contracts/api-envelope.schema.json).

Route schemas import these so ``Envelope``/``EnvelopeMeta`` are defined once.
The envelope emitter currently produces an empty ``warnings`` list, so the
element type is intentionally permissive.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EnvelopeMeta(BaseModel):
    request_id: str
    generated_at: str
    duration_ms: int
    provenance: str
    data_quality: str | None
    warnings: list[Any]
    pagination: dict[str, Any] | None
    idempotent_replay: bool


class Envelope[T](BaseModel):
    """The documented success envelope with a typed ``data`` payload."""

    ok: bool
    data: T
    error: Any = None
    meta: EnvelopeMeta


__all__ = ["Envelope", "EnvelopeMeta"]
