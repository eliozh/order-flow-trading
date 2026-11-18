"""Telegram status, test send, receipts, and token/allowlist settings routes."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Header, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.idempotency import idempotent_mutation
from oft.api.schemas.settings import mutation_openapi
from oft.api.schemas.telegram import (
    ERROR_RESPONSES,
    Envelope,
    TelegramAllowlistData,
    TelegramAllowlistUpdate,
    TelegramReceiptData,
    TelegramReceiptsData,
    TelegramSettingsData,
    TelegramSettingsUpdate,
    TelegramStatusData,
    TelegramTestRequest,
    TelegramTogglesData,
    TelegramTokenDelete,
    TelegramTokenStatusData,
    TelegramTokenUpdate,
)
from oft.application.secrets_service import SecretStoreUnavailable
from oft.application.telegram_service import (
    TelegramService,
    TelegramTokenStatus,
    receipt_contract,
)
from oft.domain.settings import SettingValidationError
from oft.ports.telegram import (
    TELEGRAM_CONFLICT,
    TELEGRAM_NOT_CONFIGURED,
    TELEGRAM_TOKEN_INVALID,
    TelegramError,
)

router = APIRouter()


def _service(request: Request) -> TelegramService:
    service = cast("TelegramService | None", request.app.state.container.telegram_service)
    if service is None:
        raise ApiError("TELEGRAM_NOT_CONFIGURED")
    return service


def _map_telegram_error(exc: TelegramError) -> ApiError:
    """Map an internal port code to the public catalog boundary."""
    if exc.code == TELEGRAM_TOKEN_INVALID:
        return ApiError("TELEGRAM_TOKEN_INVALID")
    if exc.code == TELEGRAM_NOT_CONFIGURED:
        return ApiError("TELEGRAM_NOT_CONFIGURED")
    if exc.code == TELEGRAM_CONFLICT:
        return ApiError("CONFLICT", exc.message)
    # UNAUTHORIZED, RATE_LIMITED, and UPSTREAM_ERROR are internal delivery
    # details; the API never ships a raw Bot API body or retry hint.
    return ApiError("TELEGRAM_DELIVERY_FAILED")


def _token_payload(status: TelegramTokenStatus) -> dict[str, Any]:
    return status.to_contract()


def _settings_payload(service: TelegramService, receipt_limit: int = 6) -> dict[str, Any]:
    """The aggregate payload; the token is represented by its mask only."""
    settings = service.settings_state()
    records = service.receipts(limit=receipt_limit)
    latest_test = service.latest_test_receipt()
    return {
        "token": _token_payload(service.token_status()),
        "chat_ids": list(settings.chat_ids),
        "allowed_user_ids": list(settings.allowed_user_ids),
        "toggles": TelegramTogglesData(**settings.toggles).model_dump(),
        "source": settings.source.value,
        "updated_at": settings.updated_at,
        "updated_by": settings.updated_by,
        "receipts": [receipt_contract(record) for record in records],
        "receipt_total": service.receipt_count(),
        "latest_test_receipt": (receipt_contract(latest_test) if latest_test is not None else None),
    }


@router.get(
    "/api/v1/settings/telegram",
    response_model=Envelope[TelegramSettingsData],
    responses=ERROR_RESPONSES,
)
def get_telegram_settings(request: Request) -> dict[str, Any]:
    """Token mask, allowlists, six toggles, provenance, and recent receipts."""
    data = _settings_payload(_service(request), receipt_limit=6)
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put(
    "/api/v1/settings/telegram",
    response_model=Envelope[TelegramSettingsData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def update_telegram_settings(
    payload: TelegramSettingsUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Validate and persist a partial Telegram settings patch atomically.

    Exactly one ``TELEGRAM_SETTINGS_UPDATED`` audit event is written for a real
    change; an unchanged patch writes nothing. If any toggle is enabled, at
    least one chat id is required (``chat_required_for_pushes``); an all-off
    state with no chats is allowed. The response is the same aggregate shape as
    the read, and never includes the token value.
    """
    toggles_patch = (
        payload.toggles.model_dump(exclude_unset=True) if payload.toggles is not None else None
    )

    def _mutate() -> dict[str, Any]:
        service = _service(request)
        try:
            service.update_settings(
                chat_ids=payload.chat_ids,
                allowed_user_ids=payload.allowed_user_ids,
                toggles=toggles_patch,
                actor="web",
                reason=payload.reason,
            )
        except SettingValidationError as exc:
            raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
        return _settings_payload(service)

    return idempotent_mutation(
        request,
        presented_key=idempotency_key,
        payload=payload,
        status=200,
        mutate=_mutate,
    )


@router.get(
    "/api/v1/telegram/status",
    response_model=Envelope[TelegramStatusData],
    responses=ERROR_RESPONSES,
)
def get_telegram_status(request: Request) -> dict[str, Any]:
    """The deterministic channel state; never a network call."""
    status = _service(request).status_state()
    return success_envelope(
        status.to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if status.state != "DEGRADED" else "PARTIAL",
    )


@router.post(
    "/api/v1/telegram/test",
    response_model=Envelope[TelegramReceiptData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def test_telegram(
    payload: TelegramTestRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Enqueue one test message and return its receipt.

    The token is probed through the port (``getMe``) before the enqueue, so an
    invalid token surfaces as ``TELEGRAM_TOKEN_INVALID`` rather than a queued
    message that can never be sent. No Telegram response body is returned.
    """

    def _mutate() -> dict[str, Any]:
        try:
            record = _service(request).enqueue_test(actor="web", chat_id=payload.chat_id)
        except TelegramError as exc:
            raise _map_telegram_error(exc) from exc
        return receipt_contract(record)

    return idempotent_mutation(
        request,
        presented_key=idempotency_key,
        payload=payload,
        status=200,
        mutate=_mutate,
    )


@router.get(
    "/api/v1/telegram/receipts",
    response_model=Envelope[TelegramReceiptsData],
    responses=ERROR_RESPONSES,
)
def list_telegram_receipts(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Newest-first bounded receipts; no rendered message text or token."""
    service = _service(request)
    records = service.receipts(limit=limit)
    return success_envelope(
        {
            "items": [receipt_contract(record) for record in records],
            "total": service.receipt_count(),
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if records else "MISSING",
    )


@router.put(
    "/api/v1/settings/telegram/token",
    response_model=Envelope[TelegramTokenStatusData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def set_telegram_token(
    payload: TelegramTokenUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Store or rotate the bot token; the response is the mask only."""

    def _mutate() -> dict[str, Any]:
        try:
            status = _service(request).set_token(payload.bot_token, actor="web")
        except SettingValidationError as exc:
            raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
        except SecretStoreUnavailable as exc:
            raise ApiError("CONFIG_INVALID", exc.message) from exc
        return _token_payload(status)

    return idempotent_mutation(
        request,
        presented_key=idempotency_key,
        payload=payload,
        status=200,
        mutate=_mutate,
    )


@router.delete(
    "/api/v1/settings/telegram/token",
    response_model=Envelope[TelegramTokenStatusData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def delete_telegram_token(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Delete the stored token; an environment token, if set, still applies."""

    def _mutate() -> dict[str, Any]:
        try:
            status = _service(request).delete_token(actor="web")
        except SettingValidationError as exc:
            raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
        except SecretStoreUnavailable as exc:
            raise ApiError("CONFIG_INVALID", exc.message) from exc
        return _token_payload(status)

    return idempotent_mutation(
        request,
        presented_key=idempotency_key,
        payload=TelegramTokenDelete(),
        status=200,
        mutate=_mutate,
    )


@router.get(
    "/api/v1/settings/telegram/allowlist",
    response_model=Envelope[TelegramAllowlistData],
    responses=ERROR_RESPONSES,
)
def get_telegram_allowlist(request: Request) -> dict[str, Any]:
    """The bounded chat/user allowlists and their newest provenance."""
    data = _service(request).allowlist().to_contract()
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put(
    "/api/v1/settings/telegram/allowlist",
    response_model=Envelope[TelegramAllowlistData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def update_telegram_allowlist(
    payload: TelegramAllowlistUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Validate and persist both allowlists with one audit event."""

    def _mutate() -> dict[str, Any]:
        try:
            result = _service(request).update_allowlist(
                payload.chat_ids,
                payload.allowed_user_ids,
                actor="web",
                reason=payload.reason,
            )
        except SettingValidationError as exc:
            raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
        return result.to_contract()

    return idempotent_mutation(
        request,
        presented_key=idempotency_key,
        payload=payload,
        status=200,
        mutate=_mutate,
    )


__all__ = ["router"]
