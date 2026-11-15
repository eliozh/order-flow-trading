"""Settings routes: section summaries, ``scan.period``, and risk caps."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Header, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.idempotency import idempotent_mutation
from oft.api.schemas.settings import (
    ERROR_RESPONSES,
    Envelope,
    LLMKeyDelete,
    LLMKeyStatusData,
    LLMKeyUpdate,
    LLMSettingsData,
    LLMSettingsUpdate,
    PromptSettingsData,
    RiskSettingsData,
    RiskSettingsUpdate,
    ScanSettingsData,
    ScanSettingsUpdate,
    SettingsSectionsData,
    mutation_openapi,
)
from oft.application.entry_service import RiskCapsError
from oft.application.prompt_service import (
    USER_PROMPT_ROLES,
    PromptService,
    secret_values_from_settings,
)
from oft.application.secrets_service import SecretsService, SecretStoreUnavailable
from oft.application.settings_service import SettingsService
from oft.config import ConfigError
from oft.domain.settings import SettingValidationError

router = APIRouter()
LOGGER = structlog.get_logger(__name__)


def _refresh_after_commit(request: Request) -> None:
    """Best-effort post-commit runtime refresh.

    The mutation has already committed; a refresh failure must not turn a
    successful key/document write into a 500 (which would release the key and
    cause a duplicate rotation on retry). The next provider resolution refreshes
    again anyway.
    """
    try:
        request.app.state.container.refresh_llm_settings()
    except Exception:  # noqa: BLE001 - the committed write still succeeds
        LOGGER.warning("llm_refresh_after_commit_failed", exc_info=True)


def _service(request: Request) -> SettingsService:
    container = request.app.state.container
    # The environment baseline must be the pristine pre-overlay snapshot; the
    # live ``container.settings`` holds stored keys after any refresh, which
    # would make a deleted key look like an environment key.
    base = container.llm_base_settings or container.settings
    return SettingsService(
        store=container.store,
        clock=container.clock,
        settings=base,
    )


def _secrets(request: Request) -> SecretsService:
    container = request.app.state.container
    base = container.llm_base_settings or container.settings
    return SecretsService(
        store=container.store,
        settings=base,
        clock=container.clock,
    )


def _prompt_service(request: Request) -> PromptService:
    container = request.app.state.container
    return PromptService(
        store=container.store,
        clock=container.clock,
        secret_values=secret_values_from_settings(container.settings),
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


# -- LLM provider settings and write-only keys -------------------------------


@router.get(
    "/api/v1/settings/llm",
    response_model=Envelope[LLMSettingsData],
    responses=ERROR_RESPONSES,
)
def get_llm_settings(request: Request) -> dict[str, Any]:
    """Effective provider/models/base URLs/concurrency plus masked key metadata.

    A provider row never contains a key value or prefix: ``masked`` is the fixed
    mask plus the last four characters, and an environment key reports
    ``key_source: env`` with no mask or fingerprint.
    """
    try:
        request.app.state.container.refresh_llm_settings()
        data = _service(request).read_llm_settings().to_contract()
    except (ConfigError, SecretStoreUnavailable) as exc:
        message = exc.message if hasattr(exc, "message") else str(exc)
        raise ApiError("CONFIG_INVALID", message) from exc
    return success_envelope(
        data,
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.put(
    "/api/v1/settings/llm",
    response_model=Envelope[LLMSettingsData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def update_llm_settings(
    payload: LLMSettingsUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Validate and persist a partial LLM document, auditing one real change.

    The response carries only the masked provider metadata. A save that does not
    change the effective document writes no row and no audit event.
    """
    changes = payload.model_dump(exclude_unset=True)
    reason = changes.pop("reason", "")

    def _mutate() -> dict[str, Any]:
        result = _service(request).update_llm_settings(changes, actor="web", reason=reason)
        # A committed save applies to the next runtime call (and to provider
        # rows/keys) without a restart. Best effort: the write is already
        # committed and must be reported as such.
        _refresh_after_commit(request)
        return result.to_contract()

    try:
        return idempotent_mutation(
            request,
            presented_key=idempotency_key,
            payload=payload,
            status=200,
            mutate=_mutate,
        )
    except SettingValidationError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
    except (ConfigError, SecretStoreUnavailable) as exc:
        message = exc.message if hasattr(exc, "message") else str(exc)
        raise ApiError("CONFIG_INVALID", message) from exc


@router.put(
    "/api/v1/settings/llm/{provider}/key",
    response_model=Envelope[LLMKeyStatusData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def set_llm_key(
    provider: str,
    payload: LLMKeyUpdate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Store or rotate one provider key; returns only the mask and provenance.

    The request body is write-only: it is never echoed, logged, persisted in
    SQLite, or included in an error. If no secure store is available the write is
    refused with ``CONFIG_INVALID`` and nothing is written.
    """

    def _mutate() -> dict[str, Any]:
        status = _secrets(request).set_key(provider, payload.api_key, actor="web")
        # Clear the stale in-memory overlay so a following GET/provider read
        # reflects the new stored key rather than the previous snapshot. The
        # key is already committed; a refresh failure must not report a 500.
        _refresh_after_commit(request)
        return status.to_contract()

    try:
        return idempotent_mutation(
            request,
            presented_key=idempotency_key,
            payload=payload,
            status=200,
            mutate=_mutate,
        )
    except SettingValidationError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
    except SecretStoreUnavailable as exc:
        raise ApiError("CONFIG_INVALID", exc.message) from exc


@router.delete(
    "/api/v1/settings/llm/{provider}/key",
    response_model=Envelope[LLMKeyStatusData],
    responses=ERROR_RESPONSES,
    openapi_extra=mutation_openapi(),
)
def delete_llm_key(
    provider: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """Delete one stored provider key; never falls back to another provider.

    Only the secret store entry and its metadata are removed. A same-provider
    environment key, if set, still applies; deletion does not disable it and
    does not substitute another provider. With neither a stored nor an
    environment key, decisions needing that provider return
    ``LLM_PROVIDER_NOT_CONFIGURED``. A missing key is a deterministic no-op (no
    audit); a missing secure store with metadata present is ``CONFIG_INVALID``.
    """

    def _mutate() -> dict[str, Any]:
        status = _secrets(request).delete_key(provider, actor="web")
        # Re-resolve from the pristine baseline: the deleted key must not
        # survive in the shared settings as a phantom ``env`` key. Best effort
        # so a committed delete is never reported as a failed 500.
        _refresh_after_commit(request)
        return status.to_contract()

    try:
        return idempotent_mutation(
            request,
            presented_key=idempotency_key,
            payload=LLMKeyDelete(),
            status=200,
            mutate=_mutate,
        )
    except SettingValidationError as exc:
        raise ApiError("VALIDATION_FAILED", exc.message, details=exc.details) from exc
    except SecretStoreUnavailable as exc:
        raise ApiError("CONFIG_INVALID", exc.message) from exc


# -- Prompt template aggregate (slice 36, F10b): list-only --------------------


@router.get(
    "/api/v1/settings/prompts",
    response_model=Envelope[PromptSettingsData],
    responses=ERROR_RESPONSES,
)
def list_prompt_settings(request: Request) -> dict[str, Any]:
    """The four USER prompt roles with their active version metadata.

    Deliberately list-only and read-only: no template body, hash, or credential
    is returned, and no write is accepted here. Creating, activating, and
    restoring versions stay on ``/api/v1/prompts``.
    """
    rows = _prompt_service(request).prompt_settings_rows()
    items = [row.to_contract() for row in rows]
    # A partial aggregate (1-3 of the four seeded roles) is reported as PARTIAL
    # so seeded-template data loss is visible rather than silently "COMPLETE".
    count = len(items)
    data_quality = (
        "MISSING" if count == 0 else "COMPLETE" if count == len(USER_PROMPT_ROLES) else "PARTIAL"
    )
    return success_envelope(
        {"items": items, "total": count},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=data_quality,
    )


__all__ = ["router"]
