"""Walking-skeleton integration test: config -> domain -> API."""

from __future__ import annotations

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.bootstrap import get_status
from oft.config import Settings


def test_status_envelope_matches_domain_report(settings: Settings) -> None:
    app = create_app(settings)
    expected = get_status(settings)

    with TestClient(app) as client:
        health = client.get("/health")
        response = client.get("/api/v1/status")

    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["data"] == {"status": "ok", "version": expected.version}

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["env"] == "test"
    assert body["data"]["port"] == 8765
    assert body["data"]["universe"] == expected.universe.model_dump(mode="json")
    assert body["meta"]["provenance"] == "CALC"
    assert body["meta"]["data_quality"] == "COMPLETE"
    assert body["meta"]["generated_at"] == body["data"]["generated_at"]
    assert body["data"]["generated_at"].endswith("Z")


def test_unknown_route_uses_error_envelope(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/not-a-route")

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["http_status"] == 404
