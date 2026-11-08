"""Prompt template read/activation routes (ADR-0009; no Web editor in this slice)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.idempotency import (
    complete,
    release,
    request_hash,
    reserve,
)
from oft.api.idempotency import (
    idempotency_key as require_idempotency_key,
)
from oft.api.schemas.prompt import (
    ERROR_RESPONSES,
    ActivatePromptRequest,
    EditPromptRequest,
    Envelope,
    PromptDetailData,
    PromptDiffData,
    PromptHistoryData,
    PromptListData,
    PromptVersionData,
    RestorePromptRequest,
    mutation_openapi,
)
from oft.application.prompt_service import PromptService, secret_values_from_settings

router = APIRouter()


def _service(request: Request) -> PromptService:
    container = request.app.state.container
    return PromptService(
        store=container.store,
        clock=container.clock,
        secret_values=secret_values_from_settings(container.settings),
    )


def _pagination(*, limit: int, offset: int, returned: int, total: int) -> dict[str, Any]:
    has_next = offset + returned < total
    return {
        "limit": limit,
        "cursor": str(offset),
        "has_next": has_next,
        "next_cursor": str(offset + limit) if has_next else None,
        "total": total,
    }


@router.get(
    "/api/v1/prompts",
    response_model=Envelope[PromptListData],
    responses=ERROR_RESPONSES,
)
def list_prompts(request: Request) -> dict[str, Any]:
    """List prompt template families with their explicit active version."""
    items = [asdict(summary) for summary in _service(request).list_templates()]
    return success_envelope(
        {"items": items, "total": len(items)},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
    )


@router.get(
    "/api/v1/prompts/{prompt_id}",
    response_model=Envelope[PromptDetailData],
    responses=ERROR_RESPONSES,
)
def show_prompt(prompt_id: str, request: Request) -> dict[str, Any]:
    """Show one template with its immutable version history."""
    detail = _service(request).show(prompt_id)
    return success_envelope(
        detail.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get(
    "/api/v1/prompts/{prompt_id}/history",
    response_model=Envelope[PromptHistoryData],
    responses=ERROR_RESPONSES,
)
def prompt_history(
    prompt_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List a bounded page of one template's immutable version history."""
    versions, total = _service(request).history_page(prompt_id, limit=limit, offset=offset)
    return success_envelope(
        {
            "prompt_id": prompt_id,
            "items": [version.to_contract() for version in versions],
            "total": total,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if versions else "MISSING",
        pagination=_pagination(limit=limit, offset=offset, returned=len(versions), total=total),
    )


@router.get(
    "/api/v1/prompts/{prompt_id}/diff",
    response_model=Envelope[PromptDiffData],
    responses=ERROR_RESPONSES,
)
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


@router.post(
    "/api/v1/prompts/{prompt_id}/versions",
    status_code=201,
    response_model=Envelope[PromptVersionData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def create_prompt_version(
    prompt_id: str,
    payload: EditPromptRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Create a new immutable DRAFT version (never edits an existing one)."""
    key = require_idempotency_key(idempotency_key)
    payload_hash = request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
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
        release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=201)
    return envelope


@router.post(
    "/api/v1/prompts/{prompt_id}/activate",
    response_model=Envelope[PromptVersionData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def activate_prompt_version(
    prompt_id: str,
    payload: ActivatePromptRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Explicitly switch the active version; drafts are never used implicitly."""
    key = require_idempotency_key(idempotency_key)
    payload_hash = request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
    if replay is not None:
        return replay
    try:
        version = _service(request).activate(
            prompt_id, payload.version, actor=payload.actor, reason=payload.reason
        )
    except BaseException:
        release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=200)
    return envelope


@router.post(
    "/api/v1/prompts/{prompt_id}/restore",
    status_code=201,
    response_model=Envelope[PromptVersionData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def restore_prompt_default(
    prompt_id: str,
    payload: RestorePromptRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Create a new DRAFT version that restores the built-in default body."""
    key = require_idempotency_key(idempotency_key)
    payload_hash = request_hash(payload)
    replay = reserve(request, key=key, request_hash=payload_hash)
    if replay is not None:
        return replay
    try:
        version = _service(request).restore_default(
            prompt_id, author=payload.author, reason=payload.reason
        )
    except BaseException:
        release(request, key=key)
        raise
    envelope = success_envelope(
        version.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
    complete(request, key=key, request_hash=payload_hash, envelope=envelope, status=201)
    return envelope
