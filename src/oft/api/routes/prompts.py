"""Prompt template read/activation routes (ADR-0009; no Web editor in this slice)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.prompt_service import PromptService
from oft.domain.time import utc_timestamp
from oft.ports.store import IdempotencyRecord

router = APIRouter()

_IDEMPOTENCY_LEASE = timedelta(seconds=30)
_IDEMPOTENCY_TTL = timedelta(hours=24)


class EditPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    author: str = Field(default="cli", max_length=64)
    reason: str = Field(default="", max_length=500)
    activate: bool = False
    knowledge_artifact_hash: str | None = None


class ActivatePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    actor: str = Field(default="cli", max_length=64)
    reason: str = Field(default="", max_length=500)


class RestorePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str = Field(default="cli", max_length=64)
    reason: str = Field(default="restore default", max_length=500)


def _service(request: Request) -> PromptService:
    container = request.app.state.container
    return PromptService(store=container.store, clock=container.clock)


def _scope(request: Request) -> str:
    settings = request.app.state.container.settings
    return f"{settings.env.value}:{request.method}:{request.url.path}"


def _idempotency_key(presented: str | None) -> str:
    key = (presented or "").strip()
    if not key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED")
    if len(key) > 200:
        raise ApiError("VALIDATION_FAILED", "Idempotency-Key is too long.")
    return key


def _request_hash(payload: Any) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reserve(request: Request, *, key: str, request_hash: str) -> JSONResponse | None:
    container = request.app.state.container
    now = container.clock.now()
    scope = _scope(request)
    container.store.purge_expired_idempotency(now=utc_timestamp(now))
    reservation = IdempotencyRecord(
        scope=scope,
        key=key,
        request_hash=request_hash,
        response_status=0,
        response_json="",
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + _IDEMPOTENCY_LEASE),
    )
    if container.store.reserve_idempotency(reservation):
        return None
    existing = container.store.get_idempotency(scope=scope, key=key)
    if existing is not None and existing.request_hash != request_hash:
        raise ApiError("IDEMPOTENCY_CONFLICT")
    if existing is not None and existing.response_json:
        envelope = json.loads(existing.response_json)
        envelope["meta"]["idempotent_replay"] = True
        return JSONResponse(status_code=existing.response_status, content=envelope)
    raise ApiError("IDEMPOTENCY_CONFLICT", "A request with this Idempotency-Key is still running.")


def _complete(
    request: Request, *, key: str, request_hash: str, envelope: dict[str, Any], status: int
) -> None:
    container = request.app.state.container
    now = container.clock.now()
    container.store.complete_idempotency(
        IdempotencyRecord(
            scope=_scope(request),
            key=key,
            request_hash=request_hash,
            response_status=status,
            response_json=json.dumps(envelope),
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + _IDEMPOTENCY_TTL),
        )
    )


def _release(request: Request, *, key: str) -> None:
    request.app.state.container.store.delete_idempotency(scope=_scope(request), key=key)


@router.get("/api/v1/prompts")
def list_prompts(request: Request) -> dict[str, Any]:
    """List prompt template families with their explicit active version."""
    items = [asdict(summary) for summary in _service(request).list_templates()]
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
    )


@router.get("/api/v1/prompts/{prompt_id}")
def show_prompt(prompt_id: str, request: Request) -> dict[str, Any]:
    """Show one template with its immutable version history."""
    detail = _service(request).show(prompt_id)
    return success_envelope(
        detail.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/prompts/{prompt_id}/history")
def prompt_history(prompt_id: str, request: Request) -> dict[str, Any]:
    """List every version of one template, newest history included."""
    service = _service(request)
    versions = service.history(prompt_id)
    return success_envelope(
        {
            "prompt_id": prompt_id,
            "items": [version.to_contract() for version in versions],
            "total": len(versions),
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if versions else "MISSING",
    )


@router.get("/api/v1/prompts/{prompt_id}/diff")
def prompt_diff(
    prompt_id: str,
    request: Request,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
) -> dict[str, Any]:
    """Unified diff between two immutable versions."""
    diff = _service(request).diff(prompt_id, from_version, to_version)
    return success_envelope(
        {
            "prompt_id": prompt_id,
            "from_version": from_version,
            "to_version": to_version,
            "diff": diff,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.post("/api/v1/prompts/{prompt_id}/versions")
def create_prompt_version(
    prompt_id: str,
    payload: EditPromptRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Create a new immutable DRAFT version (never edits an existing one)."""
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    try:
        version = _service(request).edit(
            prompt_id,
            payload.body,
            author=payload.author,
            reason=payload.reason,
            activate=payload.activate,
            knowledge_artifact_hash=payload.knowledge_artifact_hash,
        )
    except BaseException:
        _release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=201)
    response.status_code = 201
    return envelope


@router.post("/api/v1/prompts/{prompt_id}/activate")
def activate_prompt_version(
    prompt_id: str,
    payload: ActivatePromptRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Explicitly switch the active version; drafts are never used implicitly."""
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    try:
        version = _service(request).activate(
            prompt_id, payload.version, actor=payload.actor, reason=payload.reason
        )
    except BaseException:
        _release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=200)
    response.status_code = 200
    return envelope


@router.post("/api/v1/prompts/{prompt_id}/restore")
def restore_prompt_default(
    prompt_id: str,
    payload: RestorePromptRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Create a new DRAFT version that restores the built-in default body."""
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash(payload)
    replay = _reserve(request, key=key, request_hash=request_hash)
    if replay is not None:
        return replay
    try:
        version = _service(request).restore_default(
            prompt_id, author=payload.author, reason=payload.reason
        )
    except BaseException:
        _release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    _complete(request, key=key, request_hash=request_hash, envelope=envelope, status=201)
    response.status_code = 201
    return envelope
