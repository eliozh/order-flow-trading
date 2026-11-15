"""Settings routes: section summaries, ``scan.period``, and risk caps."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.idempotency import idempotent_mutation
from oft.api.schemas.settings import (
    ERROR_RESPONSES,
    Envelope,
    RiskSettingsData,
    RiskSettingsUpdate,
    ScanSettingsData,
    ScanSettingsUpdate,
    SettingsSectionsData,
    mutation_openapi,
)
from oft.application.entry_service import RiskCapsError
from oft.application.settings_service import SettingsService
from oft.config import ConfigError
from oft.domain.settings import SettingValidationError

router = APIRouter()


def _service(request: Request) -> SettingsService:
    container = request.app.state.container
    return SettingsService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
    )


@router.get(
    "/api/v1/settings",
    response_model=Envelope[SettingsSectionsData],
    responses=ERROR_RESPONSES,
)
def list_settings(request: Request) -> dict[str, Any]:
    """Section summaries with source and last change; values are never returned.

    A corrupt persisted risk-caps document is server configuration, so it maps
    to ``CONFIG_INVALID`` exactly like ``GET /api/v1/settings/risk`` rather than
    an untyped 500. The risk section is metadata-only: it never builds the
    utilization snapshot.
    """
    try:
        sections = _service(request).read_sections()
    except RiskCapsError as exc:
        raise ApiError("CONFIG_INVALID", exc.message) from exc
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
    """Validate and persist the scan period, auditing only a real change.

    A save that changes the effective value writes exactly one ``config`` row
    and one ``CONFIG_UPDATED`` audit event in one transaction. A save that
    repeats the already-effective value returns 200 with no new row and no
    audit event. An identical replay (same ``Idempotency-Key``, same payload)
    returns the stored envelope with ``meta.idempotent_replay=true`` and writes
    nothing; the same key with a different payload is a 409.
    """
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


@router.get(
    "/api/v1/settings/risk",
    response_model=Envelope[RiskSettingsData],
    responses=ERROR_RESPONSES,
)
def get_risk_settings(request: Request) -> dict[str, Any]:
    """Effective caps, source, defaults, utilization, and the last change.

    The utilization snapshot is read-only: it never seeds a paper account, and a
    corrupt persisted caps document fails closed as ``CONFIG_INVALID``.
    """
    try:
        data = _service(request).read_risk_settings().to_contract()
    except RiskCapsError as exc:
        raise ApiError("CONFIG_INVALID", exc.message) from exc
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put(
    "/api/v1/settings/risk",
    response_model=Envelope[RiskSettingsData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def update_risk_settings(
    payload: RiskSettingsUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Validate and persist a partial cap patch, auditing one real change.

    A stricter patch saves directly. A looser patch without ``confirm=true`` is
    a 422 with one ``confirmation_required`` detail per loosened cap and writes
    nothing. An identical replay returns the stored envelope and writes nothing;
    the same key with a different payload is a 409.
    """
    try:
        return idempotent_mutation(
            request,
            presented_key=idempotency_key,
            payload=payload,
            status=200,
            mutate=lambda: (
                _service(request)
                .update_risk_caps(
                    payload.caps,
                    actor="web",
                    reason=payload.reason,
                    confirm=payload.confirm,
                )
                .settings.to_contract()
            ),
        )
    except SettingValidationError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
    except (RiskCapsError, ConfigError) as exc:
        raise ApiError("CONFIG_INVALID", exc.message) from exc


__all__ = ["router"]
