"""Opportunity ledger: filters, detail contract, timeline, and read-only SPA surface."""

from __future__ import annotations

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


def _create_opportunity(harness: _Harness) -> tuple[str, str]:
    created = harness.client.post(
        "/api/v1/scans",
        json=PAYLOAD,
        headers={**harness.auth, "Idempotency-Key": "ledger-scan"},
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["data"]["report_id"]
    report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()["data"]
    return report_id, report["llm"]["opportunity_ids"][0]


def test_ledger_lists_filters_and_total_distinction(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id, opportunity_id = _create_opportunity(harness)
        all_states = harness.client.get("/api/v1/opportunities", headers=harness.auth)
        opened = harness.client.get(
            "/api/v1/opportunities", params={"state": "open"}, headers=harness.auth
        )
        invalidated = harness.client.get(
            "/api/v1/opportunities", params={"state": "INVALIDATED"}, headers=harness.auth
        )
    finally:
        harness.client.close()

    assert all_states.status_code == 200, all_states.text
    payload = all_states.json()["data"]
    assert payload["total"] == 1
    summary = payload["items"][0]
    assert summary["opportunity_id"] == opportunity_id
    assert summary["symbol"] == "BTCUSDT"
    assert summary["side"] in {"LONG", "SHORT"}
    assert summary["state"] == "OPEN"
    assert summary["source"]["report_id"] == report_id

    assert opened.status_code == 200
    assert [item["opportunity_id"] for item in opened.json()["data"]["items"]] == [opportunity_id]

    # The overall total stays 1 while the filter is empty, so the UI can tell
    # "no opportunity in this filter" apart from "no scan has run yet".
    assert invalidated.status_code == 200
    assert invalidated.json()["data"]["items"] == []
    assert invalidated.json()["data"]["total"] == 0
    assert all_states.json()["data"]["total"] == 1


def test_empty_ledger_reports_zero_total_not_a_filter_miss(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        listed = harness.client.get("/api/v1/opportunities", headers=harness.auth)
    finally:
        harness.client.close()

    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert data["items"] == []
    # total == 0 is the "no scan has run yet" state, distinct from an empty filter.
    assert data["total"] == 0


def test_opportunity_detail_carries_the_full_read_only_contract(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        report_id, opportunity_id = _create_opportunity(harness)
        detail = harness.client.get(f"/api/v1/opportunities/{opportunity_id}", headers=harness.auth)
        timeline = harness.client.get(
            f"/api/v1/opportunities/{opportunity_id}/timeline", headers=harness.auth
        )
        conditions = harness.client.get(
            f"/api/v1/opportunities/{opportunity_id}/conditions", headers=harness.auth
        )
    finally:
        harness.client.close()

    assert detail.status_code == 200, detail.text
    contract = detail.json()["data"]
    assert contract["opportunity_id"] == opportunity_id
    assert contract["side"] in {"LONG", "SHORT"}
    assert contract["zone"]["low"] and contract["zone"]["high"]
    assert contract["entry_preference"] in {"LIMIT", "MARKET", "STOP"}
    assert contract["trigger_conditions"]
    assert contract["invalidation_conditions"]
    assert contract["expires_at"]
    assert 0 <= contract["confidence"] <= 1
    assert contract["source"]["report_id"] == report_id
    assert contract["source"]["scan_id"]
    assert contract["provenance"]["llm"]["model"]
    assert contract["provenance"]["llm"]["prompt_version"] >= 1
    assert contract["state_history"][0]["to_state"] == "OPEN"
    assert contract["state_history"][0]["reason_code"] == "CREATED"
    for condition in contract["trigger_conditions"]:
        assert isinstance(condition["evaluable"], bool)
        assert condition["required_data"]

    assert timeline.status_code == 200
    assert timeline.json()["data"]["items"] == contract["state_history"]

    assert conditions.status_code == 200
    assert conditions.json()["data"]["opportunity_id"] == opportunity_id


def test_unknown_opportunity_is_a_404_and_no_mutation_route_exists(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        missing = harness.client.get("/api/v1/opportunities/OPP-MISSING", headers=harness.auth)
        _report_id, opportunity_id = _create_opportunity(harness)
        accept = harness.client.post(
            f"/api/v1/opportunities/{opportunity_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "no-accept"},
        )
        reject = harness.client.post(
            f"/api/v1/opportunities/{opportunity_id}/reject",
            headers={**harness.auth, "Idempotency-Key": "no-reject"},
        )
    finally:
        harness.client.close()

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert accept.status_code in {404, 405}
    assert reject.status_code in {404, 405}


def test_web_ledger_sources_are_read_only_and_label_unevaluable_conditions() -> None:
    for path in WEB_SRC.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in source, path
        assert ".innerHTML" not in source, path

    detail = (WEB_SRC / "routes" / "opportunity-detail.tsx").read_text(encoding="utf-8")
    ledger = (WEB_SRC / "routes" / "opportunities.tsx").read_text(encoding="utf-8")
    # Unevaluable conditions are labelled and never rendered as met.
    assert "UNAVAILABLE" in detail
    assert "evaluable" in detail
    assert "MISSING INPUT" in detail
    assert "never treated as met" in detail
    # The state timeline is rendered from the stored history.
    assert "state_history.map" in detail
    assert "State timeline" in detail
    # A missing source report falls back to N/A with a reports link.
    assert "sourceReport" in detail
    assert 'href="/reports"' in detail
    # Empty states distinguish "no scan yet" from "no opportunity in this filter".
    assert "No opportunities yet" in ledger
    assert "in this filter" in ledger
    assert "No {filterLabel} opportunities" in ledger
    assert 'role="tablist"' in ledger
    # Design counts: neutral, cyan, muted, danger.
    assert "count--neutral" in ledger
    assert "count--accent" in ledger
    assert "count--muted" in ledger
    assert "count--danger" in ledger
    assert "fetchOpportunity" in detail
    # 375px stays read-only on both ledger surfaces.
    assert "data-readonly={compact}" in detail
    assert "data-readonly={compact}" in ledger
    assert "Read-only at this width" in ledger
    # No mutation action exists on either ledger surface.
    assert "<button" not in detail
    for source in (detail, ledger):
        assert '"/accept"' not in source
        assert '"/reject"' not in source
        assert '"POST"' not in source


def test_opportunity_spa_routes_are_served_when_dist_exists(tmp_path: Path) -> None:
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
        _report_id, opportunity_id = _create_opportunity(harness)
        for path in ("/opportunities", f"/opportunities/{opportunity_id}"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'id="root"' in response.text
    finally:
        harness.client.close()
