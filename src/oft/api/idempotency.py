"""Shared idempotency reservation helpers for state-changing routes.

The routers own their error mapping, but the reserve/replay/complete/release
protocol is identical, so it lives here rather than in per-route copies.
``scope`` binds a key to ``env:method:path``; a replay with a different payload
is a conflict, and a second request while the first is still running is also a
conflict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.domain.time import utc_timestamp
from oft.ports.store import IdempotencyRecord

IDEMPOTENCY_LEASE = timedelta(seconds=30)
IDEMPOTENCY_TTL = timedelta(hours=24)


def scope(request: Request) -> str:
    settings = request.app.state.container.settings
    return f"{settings.env.value}:{request.method}:{request.url.path}"


def idempotency_key(presented: str | None) -> str:
    key = (presented or "").strip()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED")
    if len(key) > 200:
        raise ApiError("VALIDATION_FAILED", "Idempotency-Key is too long.")
    return key


def request_hash(payload: Any) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reserve(request: Request, *, key: str, request_hash: str) -> JSONResponse | None:
    """Reserve ``(env, method, path, key)``; return a stored replay if one exists."""
    container = request.app.state.container
    now = container.clock.now()
    scoped = scope(request)
    container.store.purge_expired_idempotency(now=utc_timestamp(now))
    reservation = IdempotencyRecord(
        scope=scoped,
        key=key,
        request_hash=request_hash,
        response_status=0,
        response_json="",
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + IDEMPOTENCY_LEASE),
    )
    if container.store.reserve_idempotency(reservation):
        return None
    existing = container.store.get_idempotency(scope=scoped, key=key)
    if existing is not None and existing.request_hash != request_hash:
        raise ApiError("IDEMPOTENCY_CONFLICT")
    if existing is not None and existing.response_json:
        envelope = json.loads(existing.response_json)
        envelope["meta"]["idempotent_replay"] = True
        return JSONResponse(status_code=existing.response_status, content=envelope)
    raise ApiError("IDEMPOTENCY_CONFLICT", "A request with this Idempotency-Key is still running.")


def complete(
    request: Request, *, key: str, request_hash: str, envelope: dict[str, Any], status: int
) -> None:
    container = request.app.state.container
    now = container.clock.now()
    container.store.complete_idempotency(
        IdempotencyRecord(
            scope=scope(request),
            key=key,
            request_hash=request_hash,
            response_status=status,
            response_json=json.dumps(envelope),
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + IDEMPOTENCY_TTL),
        )
    )


def release(request: Request, *, key: str) -> None:
    request.app.state.container.store.delete_idempotency(scope=scope(request), key=key)


def idempotent_mutation(
    request: Request,
    *,
    presented_key: str | None,
    payload: Any,
    status: int,
    mutate: Callable[[], dict[str, Any]],
    data_quality: str | Callable[[dict[str, Any]], str] = "COMPLETE",
) -> Any:
    """Run one state-changing request exactly once per ``Idempotency-Key``.

    Reserves the key, returns a stored replay when present, executes ``mutate``
    (which returns the contract data payload), records the success envelope, and
    releases the reservation if the mutation raises. ``data_quality`` may be a
    callable so a mutation whose result is degraded (for example an unavailable
    usage block) reports its own quality rather than a hard-coded ``COMPLETE``.
    """
    key = idempotency_key(presented_key)
    payload_hash = request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
    if replay is not None:
        return replay
    try:
        data = mutate()
    except BaseException:
        release(request, key=key)
        raise
    quality = data_quality(data) if callable(data_quality) else data_quality
    envelope = success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=quality,
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=status)
    return envelope


__all__ = [
    "IDEMPOTENCY_LEASE",
    "IDEMPOTENCY_TTL",
    "complete",
    "idempotency_key",
    "idempotent_mutation",
    "release",
    "request_hash",
    "reserve",
    "scope",
]
