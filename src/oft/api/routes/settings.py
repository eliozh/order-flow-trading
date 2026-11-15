"""Settings routes: section summaries plus the persisted ``scan.period``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.idempotency import idempotent_mutation
from oft.api.schemas.settings import (
    ERROR_RESPONSES,
    Envelope,
    ScanSettingsData,
    ScanSettingsUpdate,
    SettingsSectionsData,
    mutation_openapi,
)
from oft.application.settings_service import SettingsService
from oft.domain.settings import SettingValidationError

router = APIRouter()


def _service(request: Request) -> SettingsService:
    container = request.app.state.container
    return SettingsService(
        store=container.store,
        clock=container.clock,
    )


@router.get(
    "/api/v1/settings",
    response_model=Envelope[SettingsSectionsData],
    responses=ERROR_RESPONSES,
)
def list_settings(request: Request) -> dict[str, Any]:
    """Section summaries with source and last change; values are never returned."""
    sections = _service(request).read_sections()
    return success_envelope(
        {"sections": sections},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get(
    "/api/v1/settings/scan",
    response_model=Envelope[ScanSettingsData],
    responses=ERROR_RESPONSES,
)
def get_scan_settings(request: Request) -> dict[str, Any]:
    """The effective period, its source, supported values, and ``next_run``.

    ``next_run`` is the next UTC boundary of the *effective* period. A saved
    change applies at the next boundary of the schedule that was active when it
    was saved, so the daemon can apply it later than this field while pending.
    """
    return success_envelope(
        _service(request).read_scan_settings().to_contract(),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put(
    "/api/v1/settings/scan",
    response_model=Envelope[ScanSettingsData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def update_scan_settings(
    payload: ScanSettingsUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Validate, persist one row, and audit exactly one ``CONFIG_UPDATED`` event."""
    try:
        return idempotent_mutation(
            request,
            presented_key=idempotency_key,
            payload=payload,
            status=200,
            mutate=lambda: (
                _service(request).update_scan_period(payload.period, actor="web").to_contract()
            ),
        )
    except SettingValidationError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc


__all__ = ["router"]
