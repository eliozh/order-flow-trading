"""Every API/CLI response fixture validates against api-envelope.schema.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.api.app import create_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    envelope = _load("api-envelope.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(envelope, registry=registry)


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_health_ready_status_and_error_fixtures_validate(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)
    payload = {
        "fixture_path": str(FIXTURE),
        "symbol": "BTCUSDT",
        "period": "4h",
        "metrics_only": True,
    }

    with TestClient(app) as client:
        responses = [
            client.get("/health"),
            client.get("/ready"),
            client.get("/api/v1/status", headers=_auth(app)),
            client.post("/api/v1/scans", json=payload, headers=_auth(app)),
            client.get("/api/v1/scans/SCAN-MISSING", headers=_auth(app)),
            client.get("/api/v1/status"),
            client.post("/api/v1/scans", json={}, headers=_auth(app)),
            client.post(
                "/api/v1/scans",
                json=payload,
                headers={**_auth(app), "Origin": "https://evil.example"},
            ),
            client.get("/api/v1/not-a-route", headers=_auth(app)),
        ]
        scan_id = responses[3].json()["data"]["scan_id"]
        responses.append(client.get(f"/api/v1/scans/{scan_id}", headers=_auth(app)))

    for response in responses:
        _validate(validator, response.json())


def test_cli_json_envelope_validates(
    live_daemon: Settings, validator: Draft202012Validator
) -> None:
    from typer.testing import CliRunner

    from oft.cli.main import app as cli_app

    runner = CliRunner()
    run_result = runner.invoke(
        cli_app,
        [
            "scan",
            "run",
            "--fixture",
            str(FIXTURE),
            "--symbol",
            "BTCUSDT",
            "--period",
            "4h",
            "--metrics-only",
            "--json",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    run_envelope = json.loads(run_result.stdout)
    _validate(validator, run_envelope)

    status_result = runner.invoke(
        cli_app, ["scan", "status", run_envelope["data"]["scan_id"], "--json"]
    )
    assert status_result.exit_code == 0, status_result.output

    _validate(validator, json.loads(status_result.stdout))


def test_success_envelope_shape(settings: Settings, validator: Draft202012Validator) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        body = client.get("/api/v1/status", headers=_auth(app)).json()

    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert body["error"] is None
    assert body["meta"]["provenance"] == "CALC"
    _validate(validator, body)
