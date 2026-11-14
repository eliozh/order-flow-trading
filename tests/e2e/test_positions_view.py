"""E2E: the position read models behind the /positions Web surfaces.

The daemon runs in-process over a temporary data dir against the committed
archive fixtures and the deterministic ``stub`` LLM. An OPEN position is created
through the real scan -> trigger -> accept path; the list, detail, and history
payloads must expose every documented field. The empty ledger never invents a
position, and the Web sources are read-only (no journal/review UI, no live path,
no raw HTML, prompt, or secret).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
WEB_SRC = ROOT / "web" / "src"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)

POSITION_FIELDS = {
    "position_id",
    "account_id",
    "environment",
    "symbol",
    "side",
    "state",
    "size",
    "entry_price",
    "avg_entry_price",
    "mark_price",
    "notional_usd",
    "unrealized_pnl_usd",
    "unrealized_pnl_pct",
    "stop_price",
    "target_prices",
    "leverage",
    "margin_mode",
    "margin_usd",
    "risk_amount_usd",
    "risk_pct",
    "fees_usd",
    "funding_usd",
    "realized_pnl_usd",
    "opened_at",
    "updated_at",
    "closed_at",
    "exit_price",
    "source_trigger_id",
    "source_decision_id",
    "management_history",
    "data_quality",
}


@dataclass
class _Harness:
    app: Any
    client: TestClient
    auth: dict[str, str]


def _registry() -> Registry:
    """Every committed schema, so the envelope's local refs resolve offline."""
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate_envelope(envelope: dict[str, Any]) -> None:
    """The response satisfies the committed API envelope contract."""
    document = json.loads((CONTRACTS / "api-envelope.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(envelope),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


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
    return _Harness(app=app, client=client, auth=auth)


def _open_position(harness: _Harness) -> dict[str, Any]:
    scan = harness.client.post(
        "/api/v1/scans",
        json={
            "symbol": "BTCUSDT",
            "period": "4h",
            "source": "backfill",
            "metrics_only": False,
            "provider": "stub",
        },
        headers={**harness.auth, "Idempotency-Key": "positions-scan"},
    )
    assert scan.status_code == 201, scan.text
    report_id = scan.json()["data"]["report_id"]
    report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()["data"]
    opportunity_id = report["llm"]["opportunity_ids"][0]
    evaluated = harness.client.post(
        "/api/v1/triggers/evaluate",
        json={"opportunity_id": opportunity_id},
        headers={**harness.auth, "Idempotency-Key": "positions-evaluate"},
    )
    assert evaluated.status_code == 201, evaluated.text
    trigger_id = evaluated.json()["data"]["trigger"]["trigger_id"]
    recheck = harness.client.post(
        f"/api/v1/triggers/{trigger_id}/recheck",
        headers={**harness.auth, "Idempotency-Key": "positions-recheck"},
    )
    assert recheck.status_code == 200, recheck.text
    accepted = harness.client.post(
        f"/api/v1/triggers/{trigger_id}/accept",
        headers={**harness.auth, "Idempotency-Key": "positions-accept"},
    )
    assert accepted.status_code == 201, accepted.text
    return accepted.json()["data"]["position"]


def test_empty_position_ledger_is_explicit(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.client.get("/api/v1/positions", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 200, response.text
    envelope = response.json()
    _validate_envelope(envelope)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["items"] == []
    assert data["total"] == 0
    assert set(data["counts"]) == {"OPEN", "CLOSED", "LIQUIDATED", "RECONCILING"}
    assert set(data["counts"].values()) == {0}
    assert envelope["meta"]["data_quality"] == "MISSING"


def test_position_list_and_detail_expose_the_documented_contract(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        created = _open_position(harness)
        listed = harness.client.get(
            "/api/v1/positions", params={"state": "OPEN"}, headers=harness.auth
        )
        detail = harness.client.get(
            f"/api/v1/positions/{created['position_id']}", headers=harness.auth
        )
        history = harness.client.get(
            f"/api/v1/positions/{created['position_id']}/history", headers=harness.auth
        )
    finally:
        harness.client.close()

    assert listed.status_code == 200, listed.text
    list_envelope = listed.json()
    _validate_envelope(list_envelope)
    list_data = list_envelope["data"]
    assert list_envelope["meta"]["data_quality"] == "COMPLETE"
    assert list_data["total"] == 1
    assert list_data["counts"]["OPEN"] == 1
    assert list_data["counts"]["CLOSED"] == 0
    item = list_data["items"][0]
    assert set(item) >= POSITION_FIELDS
    assert item["position_id"] == created["position_id"]
    assert item["state"] == "OPEN"
    assert item["environment"] == "PAPER"
    assert item["side"] == "LONG"
    assert item["mark_price"] is not None
    # OPEN positions persist null computed PnL fields; the UI computes them.
    assert item["unrealized_pnl_usd"] is None
    assert item["unrealized_pnl_pct"] is None
    assert item["closed_at"] is None
    assert item["exit_price"] is None
    assert isinstance(item["target_prices"], list)
    assert isinstance(item["management_history"], list)
    # Entry, size, stop, leverage, margin, and risk are the recorded terms.
    assert item["size"] == created["size"]
    assert item["entry_price"] == created["entry_price"]
    assert item["avg_entry_price"] == created["avg_entry_price"]
    assert Decimal(item["size"]) > 0
    assert Decimal(item["notional_usd"]) > 0
    assert item["stop_price"] is not None and Decimal(item["stop_price"]) > 0
    assert item["leverage"] >= 1
    assert Decimal(item["margin_usd"]) > 0
    assert item["risk_pct"] > 0
    assert Decimal(item["risk_amount_usd"]) > 0
    for target in item["target_prices"]:
        assert Decimal(target) > 0
    assert item["source_trigger_id"] is not None
    assert "source_decision_id" in item
    assert item["opened_at"]
    assert set(item["data_quality"]) >= {"status", "reasons"}

    assert detail.status_code == 200, detail.text
    detail_envelope = detail.json()
    _validate_envelope(detail_envelope)
    assert detail_envelope["data"] == item
    assert detail_envelope["meta"]["data_quality"] == item["data_quality"]["status"]

    assert history.status_code == 200, history.text
    history_envelope = history.json()
    _validate_envelope(history_envelope)
    history_data = history_envelope["data"]
    assert history_data["position_id"] == created["position_id"]
    assert history_data["symbol"] == "BTCUSDT"
    assert history_data["side"] == "LONG"
    assert history_data["state"] == "OPEN"
    assert history_data["size"] == created["size"]
    assert history_data["entry_price"] == item["entry_price"]
    for key in ("events", "funding", "funding_reasons"):
        assert key in history_data
    assert isinstance(history_data["events"], list)
    assert isinstance(history_data["funding"], list)
    assert isinstance(history_data["funding_reasons"], list)
    # The timeline is ordered and every item carries the documented fields.
    sequences = [event["seq"] for event in history_data["events"]]
    assert sequences == sorted(sequences)
    for event in history_data["events"]:
        assert set(event) >= {"seq", "at", "action", "order_id", "note"}
        assert event["at"] and event["action"]
    pnl = history_data["pnl"]
    for key in (
        "basis",
        "gross_pnl_usd",
        "realized_gross_pnl_usd",
        "fees_usd",
        "funding_usd",
        "net_pnl_usd",
        "r_multiple",
        "return_pct",
        "mae_usd",
        "mfe_usd",
        "outcome_tags",
        "data_quality",
    ):
        assert key in pnl
    assert pnl["basis"] == "ACCUMULATED_GROSS"
    assert "status" in pnl["data_quality"] and "reasons" in pnl["data_quality"]
    # An OPEN position reports accumulated gross and never a fabricated net PnL.
    assert Decimal(pnl["gross_pnl_usd"]) == Decimal("0")
    assert pnl["realized_gross_pnl_usd"] == pnl["gross_pnl_usd"]
    assert pnl["net_pnl_usd"] is None
    assert pnl["r_multiple"] is None
    assert pnl["return_pct"] is None
    assert isinstance(pnl["outcome_tags"], list)
    assert history_envelope["meta"]["data_quality"] == pnl["data_quality"]["status"]


def test_positions_web_sources_are_read_only_and_safe() -> None:
    positions = (WEB_SRC / "routes" / "positions.tsx").read_text(encoding="utf-8")
    detail = (WEB_SRC / "routes" / "position-detail.tsx").read_text(encoding="utf-8")
    table = (WEB_SRC / "components" / "position-table.tsx").read_text(encoding="utf-8")
    pnl = (WEB_SRC / "components" / "pnl-breakdown.tsx").read_text(encoding="utf-8")
    timeline = (WEB_SRC / "components" / "management-timeline.tsx").read_text(encoding="utf-8")
    api = (WEB_SRC / "api" / "positions.ts").read_text(encoding="utf-8")
    source = positions + detail + table + pnl + timeline + api

    # Required labels and states.
    for label in ("未平仓持仓", "STALE", "PARTIAL", "fetchPositions", "data-readonly={compact}"):
        assert label in positions
    assert "没有未平仓持仓" in table
    assert "Paper" in table and "Live" in table
    assert "Price PnL" in pnl and "Net uPnL" in pnl and "Funding" in pnl
    assert "请求管理" in detail
    assert "fetchPositionHistory" in detail
    assert "requestManagement" in detail
    # Requesting management requires an explicit confirmation dialog and states
    # the paper-only consequence; a STALE mark disables the action.
    assert 'role="dialog"' in detail
    assert "aria-modal" in detail
    assert "确认请求管理" in detail
    assert "不会发送到交易所" in detail
    assert "管理动作已禁用" in detail
    assert "LLM 提议" in timeline and "CALC 校验" in timeline and "执行" in timeline
    assert "X-CSRF-Token" in api
    assert "Idempotency-Key" in api
    assert "oft_csrf" in api
    assert "MARK_FRESHNESS_MS" in api
    assert "state=OPEN" in api
    # The list is a labelled, keyboard-scrollable region.
    assert 'role="region"' in table
    assert "未平仓持仓表" in table
    assert "caption" in table

    # No raw HTML, prompt, or secret rendering.
    assert "dangerouslySetInnerHTML" not in source
    assert "innerHTML" not in source
    assert "raw_prompt" not in source
    assert "system_prompt" not in source
    assert "user_prompt" not in source
    assert "api_key" not in source
    assert "Authorization" not in source

    # No journal/review UI and no live control in this slice (any file). Assert
    # specific UI markers, not the bare substring "review" (which would also
    # reject innocuous words such as "preview").
    for page in (positions, detail, table, pnl, timeline, api):
        assert "/journal" not in page
        assert "JournalPage" not in page
        assert "trade-review" not in page.lower()
        assert "复盘" not in page
        assert "Enable Live" not in page
        assert "/live" not in page
        assert "/api/v1/live" not in page
