"""E2E: the paper account read models and the /paper route surface.

The account is seeded with 10,000 USDT and shows only persisted orders, fills,
and positions. There is no paper reset route or live/management action.
"""

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
WEB_SRC = ROOT / "web" / "src"
DIST = ROOT / "web" / "dist"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


@dataclass
class _Harness:
    app: Any
    client: TestClient
    auth: dict[str, str]
    trigger_id: str


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
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    app = create_app(settings)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    scan = client.post(
        "/api/v1/scans",
        json={
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        headers={**auth, "Idempotency-Key": "paper-scan"},
    )
    assert scan.status_code == 201, scan.text
    report_id = scan.json()["data"]["report_id"]
    report = client.get(f"/api/v1/reports/{report_id}", headers=auth).json()["data"]
    opportunity_id = report["llm"]["opportunity_ids"][0]
    evaluated = client.post(
        "/api/v1/triggers/evaluate",
        json={"opportunity_id": opportunity_id},
        headers={**auth, "Idempotency-Key": "paper-evaluate"},
    )
    assert evaluated.status_code == 201, evaluated.text
    trigger = evaluated.json()["data"]["trigger"]
    return _Harness(app=app, client=client, auth=auth, trigger_id=trigger["trigger_id"])


def _accept(harness: _Harness) -> dict[str, Any]:
    recheck = harness.client.post(
        f"/api/v1/triggers/{harness.trigger_id}/recheck",
        headers={**harness.auth, "Idempotency-Key": "paper-recheck"},
    )
    assert recheck.status_code == 200, recheck.text
    accepted = harness.client.post(
        f"/api/v1/triggers/{harness.trigger_id}/accept",
        headers={**harness.auth, "Idempotency-Key": "paper-accept"},
    )
    assert accepted.status_code == 201, accepted.text
    return accepted.json()["data"]


def test_empty_paper_account_is_seeded_and_shows_no_fake_fills(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        account = harness.client.get("/api/v1/paper/account", headers=harness.auth).json()["data"]
        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
        fills = harness.client.get("/api/v1/paper/fills", headers=harness.auth).json()["data"]
        equity = harness.client.get("/api/v1/paper/equity", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    assert account["environment"] == "PAPER"
    assert account["currency"] == "USDT"
    assert account["initial_balance"] == "10000.00"
    assert account["cash_balance"] == "10000.00"
    assert account["equity"] == "10000.00"
    assert account["positions"] == []
    assert account["open_positions"] == 0
    assert account["stale_inputs"] == []
    assert orders["items"] == [] and orders["total"] == 0
    assert fills["items"] == [] and fills["total"] == 0
    assert equity["equity"] == account["equity"]
    assert equity["note"]


def test_paper_read_models_match_the_accepted_order(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        accepted = _accept(harness)
        account = harness.client.get("/api/v1/paper/account", headers=harness.auth).json()["data"]
        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
        fills = harness.client.get("/api/v1/paper/fills", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    order = accepted["order"]
    fill = accepted["fill"]
    position = accepted["position"]
    assert orders["total"] == 1
    assert orders["items"][0]["order_id"] == order["order_id"]
    assert orders["items"][0]["quantity"] == order["quantity"]
    assert orders["items"][0]["status"] == "FILLED"
    assert fills["total"] == 1
    assert fills["items"][0]["order_id"] == order["order_id"]
    assert fills["items"][0]["price"] == fill["price"]
    assert account["open_positions"] == 1
    stored = account["positions"][0]
    assert stored["position_id"] == position["position_id"]
    assert stored["size"] == position["size"]
    # Equity is cash + Σ(size × recorded mark), never a fabricated PnL.
    assert account["equity"] != account["cash_balance"]
    assert stored["mark_price"] is not None


def test_paper_reset_and_live_management_routes_do_not_exist(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        reset = harness.client.post("/api/v1/paper/reset", headers=harness.auth)
        positions = harness.client.post("/api/v1/paper/positions", headers=harness.auth)
        account = harness.client.get("/api/v1/paper/account", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    assert reset.status_code in {404, 405}
    assert positions.status_code in {404, 405}
    assert account["cash_balance"] == "10000.00"


def test_paper_web_sources_are_read_only_and_label_missing_values() -> None:
    page = (WEB_SRC / "routes" / "paper.tsx").read_text(encoding="utf-8")
    tables = (WEB_SRC / "components" / "paper-tables.tsx").read_text(encoding="utf-8")

    # No reset control, no live toggle, no management action.
    assert '"/api/v1/paper/reset"' not in page
    assert "/reset" not in page
    assert "Accept" not in page
    assert "Reject" not in page
    assert "TOGGLE" not in page
    assert "<button" not in page
    # Missing marks and fees are labelled, never rendered as zero.
    assert "STALE" in tables
    assert "mark_price === null" in tables
    assert "N/A" in tables
    assert "unrealized_pnl_usd" in tables
    # The account uses the real read models, including the stale-input flag.
    assert "stale_inputs" in page
    assert "fetchPaperAccount" in page
    assert "fetchPaperOrders" in page
    assert "fetchPaperFills" in page
    assert "data-readonly={compact}" in page


def test_paper_spa_route_is_served_when_dist_exists(tmp_path: Path) -> None:
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
        response = client.get("/paper")
        assert response.status_code == 200
        assert 'id="root"' in response.text
    finally:
        harness.client.close()
