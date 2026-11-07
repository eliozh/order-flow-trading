"""Scan UI surface: the LLM stage, quality, cost, and report links reach the API."""

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
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
DIST = ROOT / "web" / "dist"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
PAYLOAD = {
    "symbol": "BTCUSDT",
    "period": "4h",
    "source": "backfill",
    "metrics_only": False,
    "provider": "stub",
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
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
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


def test_scan_detail_and_list_expose_llm_stage_cost_and_report_links(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        created = harness.client.post(
            "/api/v1/scans",
            json=PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": "e2e-scan-llm"},
        )
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        scan_id = data["scan"]["scan_id"]
        report_id = data["report_id"]

        detail = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.client.close()

    assert detail.status_code == 200, detail.text
    shown = detail.json()["data"]
    assert shown["llm_status"] in {"VALID", "REPAIRED"}
    assert shown["cost_unpriced"] is True
    assert shown["report_id"] == report_id
    assert shown["symbols"] == [
        {
            "symbol": "BTCUSDT",
            "stage": "DONE",
            "report_id": report_id,
            "data_quality": shown["data_quality"],
            "error_code": None,
            "error_message": None,
            "failed_stage": None,
            "llm_status": shown["llm_status"],
            "llm_tokens": shown["llm_tokens"],
            "cost_usd": shown["cost_usd"],
            "cost_unpriced": True,
            "data_stage": "DONE",
            "metrics_stage": "DONE",
            "llm_stage": "DONE",
            "report_stage": "DONE",
        }
    ]
    assert shown["report_links"] == [
        {"symbol": "BTCUSDT", "report_id": report_id, "href": f"/reports/{report_id}"}
    ]

    items = listed.json()["data"]["items"]
    assert items[0]["llm_status"] in {"VALID", "REPAIRED"}
    assert items[0]["report_id"] == report_id
    assert items[0]["cost_unpriced"] is True


def test_scans_spa_routes_are_served_when_dist_exists(tmp_path: Path) -> None:
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
        created = client.post(
            "/api/v1/scans",
            json=PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": "e2e-scan-ui"},
        )
        scan_id = created.json()["data"]["scan"]["scan_id"]
        for path in ("/scans", f"/scans/{scan_id}"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'id="root"' in response.text
    finally:
        harness.client.close()
