"""E2E: trigger review -> decision card -> paper accept/reject on the real API.

Everything runs offline against the committed archive fixtures and the
deterministic ``stub`` LLM provider. The resulting paper order, fill, and
position are read back independently over HTTP and must match the values the
accept response returned. No exchange, credential, or live path is touched.
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
SCAN_PAYLOAD = {
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
    opportunity_id: str
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
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"

    app = create_app(settings)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}"}

    created = client.post(
        "/api/v1/scans",
        json=SCAN_PAYLOAD,
        headers={**auth, "Idempotency-Key": "e2e-trigger-scan"},
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["data"]["report_id"]
    report = client.get(f"/api/v1/reports/{report_id}", headers=auth).json()["data"]
    opportunity_id = report["llm"]["opportunity_ids"][0]

    evaluated = client.post(
        "/api/v1/triggers/evaluate",
        json={"opportunity_id": opportunity_id},
        headers={**auth, "Idempotency-Key": "e2e-trigger-evaluate"},
    )
    assert evaluated.status_code == 201, evaluated.text
    trigger = evaluated.json()["data"]["trigger"]
    assert trigger is not None
    return _Harness(
        app=app,
        client=client,
        auth=auth,
        opportunity_id=opportunity_id,
        trigger_id=trigger["trigger_id"],
    )


def _recheck(harness: _Harness, key: str = "e2e-recheck") -> dict[str, Any]:
    response = harness.client.post(
        f"/api/v1/triggers/{harness.trigger_id}/recheck",
        headers={**harness.auth, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_accept_creates_one_paper_order_and_matches_the_read_models(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        card = _recheck(harness)
        assert card["stage_order"] == ["TRIGGER", "LLM", "RISK", "HUMAN"]
        assert card["trigger"]["provenance"] == "CALC"
        assert card["llm"]["provenance"]["provider"] == "stub"
        assert card["risk"]["provenance"] == "CALC"
        assert card["risk"]["verdict"] in {"ALLOW", "REDUCE"}
        assert card["risk"]["binding_cap"]
        allowed_quantity = card["risk"]["allowed"]["quantity"]
        assert allowed_quantity is not None and float(allowed_quantity) > 0
        # The card alone never places an order.
        assert (
            harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]["total"]
            == 0
        )

        accepted = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "e2e-accept-1"},
        )
        assert accepted.status_code == 201, accepted.text
        accepted_data = accepted.json()["data"]
        order = accepted_data["order"]
        fill = accepted_data["fill"]
        position = accepted_data["position"]
        assert order is not None and fill is not None and position is not None
        assert order["status"] == "FILLED"
        assert order["environment"] == "PAPER"
        assert fill["order_id"] == order["order_id"]
        assert fill["quantity"] == allowed_quantity
        assert position["size"] == allowed_quantity
        assert position["state"] == "OPEN"

        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
        assert orders["total"] == 1
        assert orders["items"][0]["order_id"] == order["order_id"]
        fills = harness.client.get("/api/v1/paper/fills", headers=harness.auth).json()["data"]
        assert fills["total"] == 1
        assert fills["items"][0]["fill_id"] == fill["fill_id"]
        assert fills["items"][0]["fee"] == fill["fee"]

        account = harness.client.get("/api/v1/paper/account", headers=harness.auth).json()["data"]
        assert account["environment"] == "PAPER"
        assert account["open_positions"] == 1
        assert account["positions"][0]["position_id"] == position["position_id"]
        assert account["positions"][0]["size"] == allowed_quantity
        assert account["stale_inputs"] == []

        stored = harness.client.get(
            f"/api/v1/triggers/{harness.trigger_id}", headers=harness.auth
        ).json()["data"]
        assert stored["status"] == "ACCEPTED"
        assert stored["created_order_id"] == order["order_id"]
        assert stored["entry_decision_id"] == card["decision_id"]
        assert stored["human_action"]["action"] == "ACCEPT"
    finally:
        harness.client.close()


def test_duplicate_accept_is_idempotent_and_never_creates_a_second_order(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        _recheck(harness, key="e2e-recheck-idempotent")
        first = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "e2e-accept-same-key"},
        )
        second = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "e2e-accept-same-key"},
        )
        distinct = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "e2e-accept-other-key"},
        )
        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
        fills = harness.client.get("/api/v1/paper/fills", headers=harness.auth).json()["data"]
    finally:
        harness.client.close()

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["idempotent_replay"] is True
    assert first.json()["data"]["order"]["order_id"] == second.json()["data"]["order"]["order_id"]
    # A different key after the order commit is a conflict, never a second order.
    assert distinct.status_code >= 400
    assert distinct.json()["error"]["code"] == "CONFLICT"
    assert orders["total"] == 1
    assert fills["total"] == 1


def test_reject_records_a_reason_invalidates_the_opportunity_and_keeps_the_decision(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        card = _recheck(harness, key="e2e-recheck-reject")
        rejected = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/reject",
            json={"reason": "risk structure broken"},
            headers={**harness.auth, "Idempotency-Key": "e2e-reject-1"},
        )
        assert rejected.status_code == 200, rejected.text
        data = rejected.json()["data"]
        assert data["status"] == "REJECTED"
        assert data["rejection_reason"] == "risk structure broken"
        assert data["human_action"]["action"] == "REJECT"

        trigger = harness.client.get(
            f"/api/v1/triggers/{harness.trigger_id}", headers=harness.auth
        ).json()["data"]
        assert trigger["status"] == "REJECTED"
        assert trigger["entry_decision_id"] == card["decision_id"]
        assert trigger["human_action"]["reason"] == "risk structure broken"

        opportunity = harness.client.get(
            f"/api/v1/opportunities/{harness.opportunity_id}", headers=harness.auth
        ).json()["data"]
        assert opportunity["state"] == "INVALIDATED"

        orders = harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]
        assert orders["total"] == 0
        after = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/accept",
            headers={**harness.auth, "Idempotency-Key": "e2e-accept-after-reject"},
        )
        assert after.status_code >= 400
        assert (
            harness.client.get("/api/v1/paper/orders", headers=harness.auth).json()["data"]["total"]
            == 0
        )
    finally:
        harness.client.close()


def test_reject_requires_a_non_empty_reason(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        _recheck(harness, key="e2e-recheck-empty-reason")
        empty = harness.client.post(
            f"/api/v1/triggers/{harness.trigger_id}/reject",
            json={"reason": ""},
            headers={**harness.auth, "Idempotency-Key": "e2e-reject-empty"},
        )
    finally:
        harness.client.close()

    assert empty.status_code == 422


def test_web_trigger_sources_render_text_only_and_keep_the_card_order() -> None:
    for path in WEB_SRC.rglob("*.tsx"):
        if "__tests__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in source, path
        assert ".innerHTML" not in source, path

    detail = (WEB_SRC / "routes" / "trigger-detail.tsx").read_text(encoding="utf-8")
    card = (WEB_SRC / "components" / "decision-card.tsx").read_text(encoding="utf-8")
    triggers = (WEB_SRC / "routes" / "triggers.tsx").read_text(encoding="utf-8")

    # Mandatory order: Trigger -> LLM -> Risk -> Human.
    stage_positions = [card.index(f"{index} ·") for index in (1, 2, 3, 4)]
    assert stage_positions == sorted(stage_positions)
    for label in ("Trigger", "LLM", "Risk gate", "Human action"):
        assert label in card
    assert "BLOCK" in (WEB_SRC / "api" / "triggers.ts").read_text(encoding="utf-8")
    # Accept is disabled by the shared gate, not by a hidden element.
    assert "acceptBlockedReason" in card
    # Reject needs a reason and a typed token is not required for paper accept.
    assert "请输入拒绝理由" in detail
    assert "确认接受（Paper）" in detail
    # No live/binance/credential surface exists.
    for source in (detail, card, triggers):
        lowered = source.lower()
        assert "api_key" not in lowered
        assert "live'" not in lowered
    # Both routes exist in the queue and detail wiring.
    assert "入场复核队列" in triggers
    assert "决策卡" in detail


def test_trigger_spa_routes_are_served_when_dist_exists(tmp_path: Path) -> None:
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
        _recheck(harness, key="e2e-recheck-spa")
        for path in ("/triggers", f"/triggers/{harness.trigger_id}", "/paper"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'id="root"' in response.text
    finally:
        harness.client.close()
