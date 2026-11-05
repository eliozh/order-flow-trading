"""System routes: health and status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from oft import __version__
from oft.api.envelope import new_request_id, success_envelope
from oft.bootstrap import get_status

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness check."""
    return success_envelope(
        {"status": "ok", "version": __version__},
        request_id=new_request_id(),
        provenance="NONE",
        data_quality=None,
    )


@router.get("/api/v1/status")
def status(request: Request) -> dict[str, Any]:
    """Read-only status built by the same domain path the CLI uses."""
    settings = request.app.state.settings
    report = get_status(settings)
    return success_envelope(
        report.model_dump(mode="json"),
        request_id=new_request_id(),
        generated_at=report.generated_at,
        provenance="CALC",
        data_quality="COMPLETE",
    )
