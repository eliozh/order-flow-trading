"""Scan routes: run a fixture scan and read it back."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.application.fixture_scan import get_scan, run_fixture_scan

router = APIRouter()


class ScanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_path: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    period: Literal["1m", "5m", "15m", "1h", "4h", "8h", "1d"]
    metrics_only: bool = True


def _validated_fixture_path(raw: str, fixtures_dir: Path) -> str:
    """Resolve the fixture path and reject anything outside the fixtures root."""
    root = fixtures_dir.expanduser().resolve()
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_relative_to(root):
        raise ApiError(
            "VALIDATION_FAILED",
            "fixture_path must be inside the configured fixtures directory.",
            details=[
                {
                    "field": "fixture_path",
                    "code": "path_forbidden",
                    "message": f"{raw} is outside {root}",
                }
            ],
        )
    return str(candidate)


@router.post("/api/v1/scans", status_code=201)
def create_scan(payload: ScanCreateRequest, request: Request) -> dict[str, Any]:
    """Run one fixture-backed metrics-only scan."""
    container = request.app.state.container
    fixture_path = _validated_fixture_path(payload.fixture_path, container.settings.fixtures_dir)
    record = run_fixture_scan(
        container,
        fixture_path=fixture_path,
        symbol=payload.symbol,
        period=payload.period,
        metrics_only=payload.metrics_only,
    )
    return success_envelope(
        asdict(record),
        request_id=new_request_id(),
        generated_at=record.finished_at,
        provenance="CALC",
        data_quality=record.data_quality,
    )


@router.get("/api/v1/scans/{scan_id}")
def read_scan(scan_id: str, request: Request) -> dict[str, Any]:
    container = request.app.state.container
    record = get_scan(container, scan_id)
    if record is None:
        raise ApiError("NOT_FOUND", f"Scan {scan_id} does not exist.")
    return success_envelope(
        asdict(record),
        request_id=new_request_id(),
        generated_at=record.finished_at,
        provenance="CALC",
        data_quality=record.data_quality,
    )
