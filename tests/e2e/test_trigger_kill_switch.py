"""E2E: the kill switch blocks paper orders, is immediate, and resumes only with RESUME."""

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
        headers={**auth, "Idempotency-Key": "kill-scan"},
    )
    assert scan.status_code == 201, scan.text
    report_id = scan.json()["data"]["report_id"]
    report = client.get(f"/api/v1/reports/{report_id}", headers=auth).json()["data"]
    opportunity_id = report["llm"]["opportunity_ids"][0]
    evaluated = client.post(
        "/api/v1/triggers/evaluate",
        json={"opportunity_id": opportunity_id},
        headers={**auth, "Idempotency-Key": "kill-evaluate"},
    )
    assert evaluated.status_code == 201, evaluated.text
    trigger = evaluated.json()["data"]["trigger"]
    recheck = client.post(
        f"/api/v1/triggers/{trigger['trigger_id']}/recheck",
        headers={**auth, "Idempotency-Key": "kill-recheck"},
    )
    assert recheck.status_code == 200, recheck.text
    return _Harness(app=app, client=client, auth=auth, trigger_id=trigger["trigger_id"])


def test_kill_switch_on_is_immediate_and_blocks_paper_orders(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        status = harness.client.get("/api/v1/kill-switch", headers=harness.auth)
        assert status.status_code == 200
        assert status.json()["data"]["active"] is False

        activated = harness.client.post("/api/v1/kill-switch", headers=harness.auth)
        assert activated.status_code == 200, activated.text
        assert activated.json()["data"]["active"] is True

        blocked = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "kill-accept-blocked"},
        )
        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    assert blocked.status_code >= 400
    assert orders["total"] == 0


def test_kill_switch_off_requires_the_typed_resume_token(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        harness.client.post("/api/v1/kill-switch", headers=harness.auth)

        missing = harness.client.delete("/api/v1/kill-switch", headers=harness.auth)
        wrong = harness.client.delete(
            "/api/v1/kill-switch", params={"token": "resume"}, headers=harness.auth
        )
        still_on = harness.client.get("/api/v1/kill-switch", headers=harness.auth).json()["data"]

        resumed = harness.client.delete(
            "/api/v1/kill-switch", params={"token": "RESUME"}, headers=harness.auth
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["data"]["active"] is False

        accepted = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "kill-accept-resumed"},
        )
        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    assert missing.status_code == 422
    assert wrong.status_code == 422
    assert still_on["active"] is True
    assert accepted.status_code == 201, accepted.text
    assert orders["total"] == 1


def test_kill_switch_is_global_and_available_at_every_width() -> None:
    app = (WEB_SRC / "App.tsx").read_text(encoding="utf-8")
    component = (WEB_SRC / "components" / "kill-switch.tsx").read_text(encoding="utf-8")

    # The switch is rendered outside the route switch, so every route keeps it.
    assert "<KillSwitchBar />" in app
    app_body = app.split("<main", 1)[1]
    assert "<KillSwitchBar />" not in app_body
    # Resuming needs the exact typed token; it is not a hidden compact action.
    assert "'RESUME'" in component
    assert "token !== 'RESUME'" in component
    assert "isCompact" not in component
    assert "compact" not in component.lower()
    # Stopping is immediate: no confirmation dialog opens before activate().
    assert "void activate()" in component


def test_no_live_credential_or_exchange_path_is_reachable() -> None:
    for name in (
        "routes/triggers.tsx",
        "routes/trigger-detail.tsx",
        "routes/paper.tsx",
        "components/decision-card.tsx",
        "components/risk-panel.tsx",
        "components/paper-tables.tsx",
        "components/kill-switch.tsx",
    ):
        lowered = (WEB_SRC / name).read_text(encoding="utf-8").lower()
        assert "api_key" not in lowered, name
        assert "api-secret" not in lowered, name
        # No web Telegram surface exists yet (slice 47 ships the CLI/daemon
        # path only); the browser must not grow one accidentally.
        assert "telegram" not in lowered, name
        assert "livebroker" not in lowered, name
