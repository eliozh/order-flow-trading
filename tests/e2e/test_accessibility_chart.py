"""Accessibility contract for the metrics-only chart (design A2/A4/A8)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
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


def _chart_source() -> str:
    return (WEB_SRC / "components" / "chart.tsx").read_text(encoding="utf-8")


def test_chart_source_has_keyboard_and_semantic_contract() -> None:
    source = _chart_source()
    assert "<table" in source
    assert "View data table" in source or "data table" in source.lower()
    assert "aria-label" in source
    assert "onKeyDown" in source
    assert "role=" in source
    assert "tabIndex" in source or "tabindex" in source


def test_chart_source_has_no_raw_html_injection() -> None:
    for path in WEB_SRC.rglob("*.tsx"):
        assert "dangerouslySetInnerHTML" not in path.read_text(encoding="utf-8"), path


def test_report_quality_and_llm_reason_are_explicit(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        created = harness.client.post(
            "/api/v1/scans",
            json=PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": "a11y"},
        )
        assert created.status_code == 201, created.text
        report_id = created.json()["data"]["report_id"]
        response = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["data_quality"]
    assert data["llm"] == {
        "configured": False,
        "reason": data["llm"]["reason"],
    }
    assert "not configured" in data["llm"]["reason"].lower()
    serialized = json.dumps(data)
    assert "<script" not in serialized.lower()


def test_vp_bin_alpha_meets_the_design_contrast_floor() -> None:
    import re

    css = (WEB_SRC / "styles" / "app.css").read_text(encoding="utf-8")
    match = re.search(r"\.vp-bin\s*\{[^}]*opacity:\s*([0-9.]+)", css)
    assert match is not None
    assert float(match.group(1)) >= 0.65
