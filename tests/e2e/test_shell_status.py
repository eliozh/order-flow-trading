"""E2E shell status: the data surface the /system screen renders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"
DIST = ROOT / "web" / "dist"


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def test_system_surface_reports_health_readiness_and_scan(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["data"]["status"] == "ok"

        scan = client.post(
            "/api/v1/scans",
            json={
                "fixture_path": str(FIXTURE),
                "symbol": "BTCUSDT",
                "period": "4h",
                "metrics_only": True,
            },
            headers=_auth(app),
        )
        assert scan.status_code == 201, scan.text
        scan_data = scan.json()["data"]

        ready = client.get("/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["ok"] is True
        data = body["data"]

        assert data["status"] == "ready"
        assert data["version"]
        assert data["data_dir"] == str(settings.data_dir)
        names = {check["name"] for check in data["checks"]}
        assert {"config", "storage.sqlite", "storage.parquet", "clock", "market_data"} <= names
        assert all(check["status"] == "ok" for check in data["checks"])

        latest = data["latest_scan"]
        assert latest["scan_id"] == scan_data["scan_id"]
        assert latest["delta"] == "1.50"
        assert latest["symbol"] == "BTCUSDT"

        fetched = client.get(f"/api/v1/scans/{scan_data['scan_id']}", headers=_auth(app))
        assert fetched.json()["data"]["delta"] == latest["delta"]


def test_built_shell_serves_the_system_route(settings: Settings) -> None:
    if not (DIST / "index.html").is_file():
        import pytest

        pytest.skip("web/dist not built; build the SPA to exercise static serving")

    app = create_app(settings)
    with TestClient(app) as client:
        session = client.get(
            "/", params={"bootstrap": app.state.browser_bootstrap}, follow_redirects=False
        )
        assert session.status_code == 302
        response = client.get("/system")

    assert response.status_code == 200
    assert 'id="root"' in response.text
