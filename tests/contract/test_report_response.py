"""Contract checks for the metrics-only report response and response hardening."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
PAYLOAD = {
    "symbol": "BTCUSDT",
    "period": "4h",
    "source": "backfill",
    "metrics_only": True,
}


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
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validate(name: str, payload: dict[str, Any]) -> None:
    common = _load("common.schema.json")
    metrics_schema = _load("orderflow-metrics.schema.json")
    registry = (
        Registry()
        .with_resource(
            common["$id"], Resource.from_contents(common, default_specification=DRAFT202012)
        )
        .with_resource(
            metrics_schema["$id"],
            Resource.from_contents(metrics_schema, default_specification=DRAFT202012),
        )
    )
    validator = Draft202012Validator(_load(name), registry=registry)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _create_report(harness: _Harness) -> dict[str, Any]:
    response = harness.client.post(
        "/api/v1/scans",
        json=PAYLOAD,
        headers={**harness.auth, "Idempotency-Key": "contract-key"},
    )
    assert response.status_code == 201, response.text
    report_id = response.json()["data"]["report_id"]
    report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    assert report.status_code == 200, report.text
    return report.json()


def test_report_embeds_contract_valid_snapshot_and_metrics(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        body = _create_report(harness)
    finally:
        harness.client.close()

    assert body["ok"] is True
    data = body["data"]
    _validate("market-snapshot.schema.json", data["snapshot"])
    _validate("orderflow-metrics.schema.json", data["metrics"])
    assert data["snapshot"]["orderflow"] == data["metrics"]
    assert data["snapshot"]["symbol"] == "BTCUSDT"
    assert data["snapshot"]["timeframe"] == "4h"
    assert data["snapshot"]["source_mode"] == "BACKFILL"


def test_csp_has_no_unsafe_inline_or_eval(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")

    csp = response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp


def test_frontend_never_injects_raw_html() -> None:
    offenders: list[str] = []
    for path in (ROOT / "web" / "src").rglob("*.tsx"):
        if "dangerouslySetInnerHTML" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_error_envelopes_do_not_reflect_untrusted_html(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json={**PAYLOAD, "symbol": "<img src=x onerror=alert(1)>"},
            headers={**harness.auth, "Idempotency-Key": "hostile"},
        )
    finally:
        harness.client.close()

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    serialized = json.dumps(body)
    assert "<img" not in serialized


@pytest.mark.parametrize("header", ["", "   "])
def test_blank_idempotency_key_is_rejected(tmp_path: Path, header: str) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json=PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": header},
        )
    finally:
        harness.client.close()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
