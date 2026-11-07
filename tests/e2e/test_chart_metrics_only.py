"""E2E surface for the metrics-only chart: report API, SPA routes, and responsive shell."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
DIST = ROOT / "web" / "dist"
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
        env="test", port=8765, data_dir=tmp_path / "data", archive_fixture_dir=ARCHIVE
    )
    container = build_container(settings, initialize=True)
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    app = create_app(settings)
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _report(harness: _Harness) -> dict[str, Any]:
    response = harness.client.post(
        "/api/v1/scans",
        json=PAYLOAD,
        headers={**harness.auth, "Idempotency-Key": "e2e-chart"},
    )
    assert response.status_code == 201, response.text
    report_id = response.json()["data"]["report_id"]
    report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    assert report.status_code == 200, report.text
    return report.json()["data"]


def test_report_surface_carries_chart_series_for_one_symbol_and_period(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        data = _report(harness)
    finally:
        harness.client.close()

    metrics = data["metrics"]
    bars = data["snapshot"]["bars"]
    assert data["symbol"] == "BTCUSDT"
    assert data["timeframe"] == "4h"
    assert bars and metrics["volume_profile"]["bins"] and metrics["footprint"]["bars"]
    assert metrics["cvd"]["points"]
    footprint_starts = {bar["bar_start"] for bar in metrics["footprint"]["bars"]}
    assert {bar["bar_start"] for bar in bars} <= footprint_starts
    assert data["data_quality"] in {"COMPLETE", "PARTIAL", "ESTIMATED"}
    assert data["llm"]["configured"] is False


def test_reports_spa_route_is_served_when_dist_exists(tmp_path: Path) -> None:
    if not (DIST / "index.html").is_file():
        import pytest

        pytest.skip("web/dist not built; build the SPA to exercise static serving")

    harness = _harness(tmp_path)
    try:
        client = harness.client
        session = client.get(
            "/",
            params={"bootstrap": harness.app.state.browser_bootstrap},
            follow_redirects=False,
        )
        assert session.status_code == 302
        report_id = _report(harness)["report_id"]
        for path in ("/reports", f"/reports/{report_id}", "/scans"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'id="root"' in response.text
    finally:
        harness.client.close()
