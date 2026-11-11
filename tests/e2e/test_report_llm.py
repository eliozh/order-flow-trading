"""Report LLM band: payload provenance, rejected narrative, and SPA safety."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"
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


def _harness(tmp_path: Path, fixtures_dir: Path = FIXTURES) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=fixtures_dir,
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


def _scan(harness: _Harness, *, metrics_only: bool, key: str) -> dict[str, Any]:
    created = harness.client.post(
        "/api/v1/scans",
        json={**PAYLOAD, "metrics_only": metrics_only},
        headers={**harness.auth, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


def test_report_payload_carries_the_llm_band_and_opportunity_links(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        scan = _scan(harness, metrics_only=False, key="report-llm-band")
        report_id = scan["report_id"]
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
        opportunity_ids = report.json()["data"]["llm"]["opportunity_ids"]
        opportunity = harness.client.get(
            f"/api/v1/opportunities/{opportunity_ids[0]}", headers=harness.auth
        )
    finally:
        harness.client.close()

    assert report.status_code == 200, report.text
    data = report.json()["data"]
    llm = data["llm"]
    assert data["llm_stage"] == "DONE"
    assert llm["configured"] is True
    assert llm["validation_status"] in {"VALID", "REPAIRED"}
    assert llm["provider"] == "stub"
    assert llm["model"] == "fixture"
    assert llm["prompt_id"] == "analysis"
    assert llm["prompt_version"] >= 1
    assert isinstance(llm["narrative"], str) and llm["narrative"]
    assert isinstance(llm["confidence"], (int, float))
    assert "status" in llm["knowledge"]
    assert llm["cost"]["unpriced"] is True
    assert llm["usage"]["total_tokens"] >= 0
    assert opportunity_ids and len(opportunity_ids) == 1

    assert opportunity.status_code == 200, opportunity.text
    contract = opportunity.json()["data"]
    assert contract["source"]["report_id"] == report_id
    assert contract["provenance"]["llm"]["model"] == llm["model"]
    assert contract["provenance"]["llm"]["prompt_version"] == llm["prompt_version"]


def test_metrics_only_report_is_an_explicit_calc_only_llm_band(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        scan = _scan(harness, metrics_only=True, key="report-calc-only")
        report = harness.client.get(
            f"/api/v1/reports/{scan['report_id']}", headers=harness.auth
        ).json()["data"]
    finally:
        harness.client.close()

    assert report["llm_stage"] == "SKIPPED"
    assert report["llm"]["configured"] is False
    assert report["llm"]["reason"]
    assert report["metrics"]["cvd"]["points"]
    assert report["snapshot"]["bars"]


def test_rejected_narrative_stays_explicit_and_the_calc_report_stays_readable(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "llm-fixtures"
    shutil.copytree(FIXTURES, fixtures)
    # The stub ANALYSIS role reads opportunity_valid.json; replacing it with a
    # schema-invalid document forces a real REJECTED narrative end to end.
    shutil.copyfile(fixtures / "invalid_schema.json", fixtures / "opportunity_valid.json")

    harness = _harness(tmp_path, fixtures_dir=fixtures)
    try:
        scan = _scan(harness, metrics_only=False, key="report-rejected")
        report = harness.client.get(
            f"/api/v1/reports/{scan['report_id']}", headers=harness.auth
        ).json()["data"]
    finally:
        harness.client.close()

    llm = report["llm"]
    assert report["llm_stage"] == "FAILED"
    assert llm["configured"] is True
    assert llm["validation_status"] == "REJECTED"
    assert llm["narrative"] in {None, ""}
    # The failure reason names the offending schema field.
    assert "SCHEMA_INVALID" in llm["reason"]
    assert "subject" in llm["reason"]
    assert llm["opportunity_ids"] == []
    # Metrics, snapshot, and provenance survive a narrative failure.
    assert report["metrics"]["cvd"]["points"]
    assert report["snapshot"]["bars"]
    assert llm["provider"] == "stub"
    assert llm["model"] == "fixture"
    assert llm["prompt_version"] >= 1

    llm_band = (WEB_SRC / "components" / "llm-band.tsx").read_text(encoding="utf-8")
    detail = (WEB_SRC / "routes" / "report-detail.tsx").read_text(encoding="utf-8")
    assert "Narrative REJECTED." in llm_band
    assert "No narrative was produced. The CALC report remains valid and readable." in llm_band
    # CALC stays first and the LLM band never hides the report.
    assert detail.index('aria-labelledby="calc-heading"') < detail.index("<LlmBand")
    assert detail.index("<Chart") < detail.index("<LlmBand")


def test_report_detail_is_calc_first_read_only_and_has_no_action() -> None:
    detail = (WEB_SRC / "routes" / "report-detail.tsx").read_text(encoding="utf-8")
    card = (WEB_SRC / "components" / "opportunity-card.tsx").read_text(encoding="utf-8")

    # CALC band, then chart, then the LLM band in that order.
    assert detail.index("CALC · deterministic metrics") < detail.index("<LlmBand")
    # The report offers no mutation action; it only links to the ledger.
    assert "there is no accept or reject action here" in detail
    assert "<button" not in detail
    assert '"/accept"' not in detail
    assert '"/reject"' not in detail
    assert '"POST"' not in detail
    assert "fetchOpportunity" in detail
    assert "/opportunities/" in card
    assert "<button" not in card
    assert '"/accept"' not in card
    assert '"/reject"' not in card

    # The shipped bundle carries the compact breakpoint and read-only marker,
    # so the 375px surface is served read-only from production assets.
    if (DIST / "index.html").is_file():
        bundle = "\n".join(
            path.read_text(encoding="utf-8") for path in (DIST / "assets").glob("*.js")
        )
        assert "data-readonly" in bundle
        assert "max-width: 768px" in bundle
        assert "Read-only at this width" in bundle


def test_report_surfaces_are_read_only_and_escape_narrative(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        scan = _scan(harness, metrics_only=False, key="report-safety")
        report = harness.client.get(
            f"/api/v1/reports/{scan['report_id']}", headers=harness.auth
        ).json()["data"]
    finally:
        harness.client.close()

    serialized = str(report)
    assert "<script" not in serialized.lower()
    assert "api_key" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_web_report_sources_render_text_nodes_only() -> None:
    for path in WEB_SRC.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in source, path
        assert ".innerHTML" not in source, path

    detail = (WEB_SRC / "routes" / "report-detail.tsx").read_text(encoding="utf-8")
    llm_band = (WEB_SRC / "components" / "llm-band.tsx").read_text(encoding="utf-8")
    assert 'ProvenanceStamp kind="LLM"' in llm_band
    assert "data-readonly={compact}" in detail
    assert "No narrative was produced" in llm_band
    assert "not a measurement" in llm_band.lower()


def test_report_spa_route_and_csp_headers(tmp_path: Path) -> None:
    if not (DIST / "index.html").is_file():
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
        scan = _scan(harness, metrics_only=False, key="report-spa")
        response = client.get(f"/reports/{scan['report_id']}")
    finally:
        harness.client.close()

    assert response.status_code == 200
    assert 'id="root"' in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
