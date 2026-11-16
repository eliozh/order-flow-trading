"""Contract: the report export body validates against the shared schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

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
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate(name: str, payload: dict[str, Any]) -> None:
    validator = Draft202012Validator(_load(name), registry=_registry())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _create_report(harness: _Harness) -> str:
    response = harness.client.post(
        "/api/v1/scans",
        json=PAYLOAD,
        headers={**harness.auth, "Idempotency-Key": "export-contract-key"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["report_id"])


def test_export_content_types_and_disposition(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        markdown = harness.client.get(
            f"/api/v1/reports/{report_id}/export", params={"format": "md"}, headers=harness.auth
        )
        exported_json = harness.client.get(
            f"/api/v1/reports/{report_id}/export", params={"format": "json"}, headers=harness.auth
        )
    finally:
        harness.client.close()

    assert markdown.status_code == 200
    assert markdown.headers["content-type"] == "text/markdown; charset=utf-8"
    assert (
        markdown.headers["content-disposition"] == f'attachment; filename="report-{report_id}.md"'
    )

    assert exported_json.status_code == 200
    assert exported_json.headers["content-type"] == "application/json"
    assert (
        exported_json.headers["content-disposition"]
        == f'attachment; filename="report-{report_id}.json"'
    )


def test_json_export_validates_as_the_envelope_with_contract_sections(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        response = harness.client.get(
            f"/api/v1/reports/{report_id}/export", params={"format": "json"}, headers=harness.auth
        )
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    body = json.loads(response.text)
    _validate("api-envelope.schema.json", body)
    assert body["ok"] is True
    assert body["data"]["report_id"] == report_id
    _validate("market-snapshot.schema.json", body["data"]["snapshot"])
    _validate("orderflow-metrics.schema.json", body["data"]["metrics"])


def test_invalid_format_error_envelope_validates(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id = _create_report(harness)
        response = harness.client.get(
            f"/api/v1/reports/{report_id}/export",
            params={"format": "csv"},
            headers=harness.auth,
        )
    finally:
        harness.client.close()

    assert response.status_code == 422
    body = response.json()
    _validate("api-envelope.schema.json", body)
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_FAILED"


def test_the_export_route_exposes_no_caller_path_parameter(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        schema = harness.client.get("/api/openapi.json").json()
    finally:
        harness.client.close()

    operation = schema["paths"]["/api/v1/reports/{report_id}/export"]["get"]
    parameters = operation["parameters"]
    path_parameters = [item for item in parameters if item["in"] == "path"]
    query_parameters = [item for item in parameters if item["in"] == "query"]

    assert [item["name"] for item in path_parameters] == ["report_id"]
    assert [item["name"] for item in query_parameters] == ["format"]
    assert set(query_parameters[0]["schema"]["enum"]) == {"md", "json"}
    assert query_parameters[0]["schema"]["default"] == "md"

    success = operation["responses"]["200"]
    assert set(success["content"]) == {"text/markdown", "application/json"}
    assert success["content"]["text/markdown"]["schema"] == {"type": "string"}
    assert success["content"]["application/json"]["schema"] == {"type": "object"}
    assert {name.lower() for name in success["headers"]} >= {
        "content-disposition",
        "x-oft-calc-blocks",
        "x-oft-llm-blocks",
    }
    assert operation["responses"]["404"]["description"].startswith("NOT_FOUND")
    assert operation["responses"]["422"]["description"].startswith("VALIDATION_FAILED")
