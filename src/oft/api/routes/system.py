"""System routes: health, readiness, and status."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import error_response
from oft.application.health import build_readiness
from oft.application.jobs import latest_jobs_payload
from oft.application.status import build_status

router = APIRouter()

# A failed readiness check maps to the catalog code for its failure group.
_CHECK_ERROR_CODES = {
    "config": "CONFIG_INVALID",
    "storage.sqlite": "STORAGE_UNAVAILABLE",
    "storage.parquet": "STORAGE_UNAVAILABLE",
    "clock": "CLOCK_DRIFT",
    "market_data": "DATA_UNAVAILABLE",
}


def readiness_error_code(checks: tuple[Any, ...]) -> str:
    """Return the catalog code for the first failed readiness check group."""
    for check in checks:
        if check.status != "ok":
            return _CHECK_ERROR_CODES.get(check.name, "INTERNAL_ERROR")
    return "INTERNAL_ERROR"


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness check."""
    from oft.application.health import build_health

    report = build_health()
    return success_envelope(
        report.model_dump(mode="json"),
        request_id=new_request_id(),
        provenance="NONE",
        data_quality=None,
    )


@router.get("/ready")
def ready(request: Request) -> Any:
    """Unauthenticated readiness probe with named checks."""
    container = request.app.state.container
    report = build_readiness(
        container.settings,
        container.store,
        container.market_data,
        container.clock,
    )
    payload = asdict(report)
    if report.status != "ready":
        details = [
            {"field": check.name, "code": check.status, "message": check.detail}
            for check in report.checks
            if check.status != "ok"
        ]
        return error_response(
            readiness_error_code(report.checks),
            message="One or more readiness checks failed.",
            details=details,
        )
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=report.checked_at,
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.get("/api/v1/status")
def status(request: Request) -> dict[str, Any]:
    """Read-only status built by the same domain path the CLI uses."""
    container = request.app.state.container
    report = build_status(
        container.settings,
        container.market_data,
        container.clock,
        store=container.store,
    )
    data = report.model_dump(mode="json")
    # Additive slice-26 job surface; no existing status field is renamed or
    # removed and the envelope is unchanged. The shared helper aggregates the
    # latest boundary window, so per-position rows cannot disagree with it.
    data["jobs"] = latest_jobs_payload(container.store)
    return success_envelope(
        data,
        request_id=new_request_id(),
        generated_at=report.generated_at,
        provenance="CALC",
        data_quality="COMPLETE",
    )
