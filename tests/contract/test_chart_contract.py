"""Contract checks for the chart-panel payload and its error envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from oft.api.app import create_app
from oft.api.schemas.chart import ChartPayload
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    app = create_app(settings)
    client = TestClient(app)
    return _Harness(
        settings=settings,
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _scan(harness: _Harness, period: str, key: str) -> str:
    response = harness.client.post(
        "/api/v1/scans",
        json={
            "symbol": "BTCUSDT",
            "period": period,
            "source": "backfill",
            "metrics_only": True,
        },
        headers={**harness.auth, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["report_id"]


def _chart(harness: _Harness, report_id: str, period: str | None = None) -> Any:
    params = {} if period is None else {"period": period}
    return harness.client.get(
        f"/api/v1/reports/{report_id}/chart", params=params, headers=harness.auth
    )


def test_available_payload_matches_the_schema_and_its_source_report(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "contract-available")
        hourly_id = _scan(harness, "1h", "contract-available-1h")
        native_body = _chart(harness, report_id).json()
        switched_body = _chart(harness, report_id, period="1h").json()
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()[
            "data"
        ]
        hourly = harness.client.get(f"/api/v1/reports/{hourly_id}", headers=harness.auth).json()[
            "data"
        ]
    finally:
        harness.client.close()

    payload = ChartPayload.model_validate(native_body["data"])
    assert payload.status == "AVAILABLE"
    assert payload.bars == report["snapshot"]["bars"]
    assert payload.metrics == report["metrics"]
    assert payload.bars
    assert payload.metrics is not None and payload.metrics["cvd"]["points"]
    assert payload.native_period == "4h"
    assert payload.requested_period == "4h"
    assert payload.source_report_id == report_id
    assert payload.generated_at == report["generated_at"]
    assert payload.reason is None
    assert payload.available_periods[0] == payload.native_period
    assert len(set(payload.available_periods)) == len(payload.available_periods)

    switched = ChartPayload.model_validate(switched_body["data"])
    assert switched.status == "AVAILABLE"
    assert switched.source_report_id == hourly_id
    assert switched.bars == hourly["snapshot"]["bars"]
    assert switched.metrics == hourly["metrics"]


def test_missing_payload_has_no_bars_or_metrics_and_a_reason(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "contract-missing")
        body = _chart(harness, report_id, period="15m").json()
    finally:
        harness.client.close()

    payload = ChartPayload.model_validate(body["data"])
    assert payload.status == "MISSING"
    assert payload.bars == []
    assert payload.metrics is None
    assert payload.source_report_id is None
    assert payload.generated_at is None
    assert payload.reason
    assert "15m" in payload.reason
    assert payload.available_periods == ["4h"]
    assert payload.data_quality == "MISSING"


def test_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "contract-extra")
        payload = _chart(harness, report_id).json()["data"]
    finally:
        harness.client.close()

    with pytest.raises(ValidationError):
        ChartPayload.model_validate({**payload, "unexpected_field": True})


def test_error_envelopes_use_the_catalog_codes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _scan(harness, "4h", "contract-errors")
        unknown = _chart(harness, "RPT-UNKNOWN")
        invalid = _chart(harness, report_id, period="8h")
    finally:
        harness.client.close()

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert unknown_body["ok"] is False
    assert unknown_body["error"]["code"] == "NOT_FOUND"
    assert unknown_body["error"]["http_status"] == 404
    assert unknown_body["error"]["category"] == "VALIDATION"

    assert invalid.status_code == 422
    invalid_body = invalid.json()
    assert invalid_body["ok"] is False
    assert invalid_body["error"]["code"] == "VALIDATION_FAILED"
    assert invalid_body["error"]["http_status"] == 422
    assert invalid_body["error"]["retryable"] is False
