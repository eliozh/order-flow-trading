"""Contract: trigger list provenance, the decision read, and journal envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import _helpers
from _registry import validate
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application.position_service import PositionService
from oft.clock import FixedClock
from position_support import set_position_mark


def _app(loop: dict[str, Any]) -> tuple[TestClient, dict[str, str]]:
    app = create_app(loop["container"].settings, clock=FixedClock(_helpers.NOW))
    return TestClient(app), {"Authorization": f"Bearer {app.state.api_token}"}


def _close_position(loop: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    container = loop["container"]
    position = loop["result"].position
    assert position is not None
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        position["updated_at"],
    )
    monitored = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    ).monitor(position["position_id"])
    assert monitored.trade_log is not None
    return position, monitored.trade_log


def test_the_trigger_list_envelope_and_projection_validate(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    trigger_id = str(loop["trigger"]["trigger_id"])
    client, auth = _app(loop)

    with client:
        response = client.get("/api/v1/triggers", headers=auth)
        decision = client.get(f"/api/v1/triggers/{trigger_id}/decision", headers=auth)

    assert response.status_code == 200, response.text
    envelope = response.json()
    validate("api-envelope.schema.json", envelope)
    item = envelope["data"]["items"][0]
    assert item["trigger_id"] == trigger_id
    assert item["environment"] == "PAPER"
    assert isinstance(item["risk_verdict"], dict)
    assert item["risk_verdict"]["verdict"] in {"ALLOW", "REDUCE", "BLOCK"}
    assert item["confidence"] == loop["decision"]["confidence"]

    stored = loop["container"].store.get_trigger_event(trigger_id)
    assert stored is not None
    stored_payload = json.loads(stored.payload_json)
    validate("trigger-event.schema.json", stored_payload)
    assert "confidence" not in stored_payload

    assert decision.status_code == 200, decision.text
    decision_envelope = decision.json()
    validate("api-envelope.schema.json", decision_envelope)
    assert decision_envelope["meta"]["provenance"] == "MIXED"
    assert decision_envelope["data"]["status"] == "FOUND"
    provenance = decision_envelope["data"]["provenance"]
    assert provenance["provider"] == "stub"
    for field in ("model", "prompt_id", "prompt_version", "prompt_hash", "cost", "recorded_at"):
        assert field in provenance


def test_the_journal_stats_and_export_envelopes_validate(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    client, auth = _app(loop)

    with client:
        stats = client.get("/api/v1/journal/stats?period=8w", headers=auth)
        exported = client.get("/api/v1/journal/export?format=csv", headers=auth)

    assert stats.status_code == 200, stats.text
    stats_envelope = stats.json()
    validate("api-envelope.schema.json", stats_envelope)
    stats_data = stats_envelope["data"]
    assert stats_data["status"] == "UNDEFINED"
    assert stats_envelope["meta"]["provenance"] == "CALC"
    assert stats_envelope["meta"]["data_quality"] == "MISSING"
    # The typed response model exposes the documented nested shapes.
    assert set(stats_data) == {
        "status",
        "reason",
        "period",
        "sample_size",
        "denominator",
        "exclusions",
        "definitions",
        "wins",
        "losses",
        "ties",
        "win_rate",
        "expectancy_usd",
        "avg_r",
        "totals",
        "data_quality",
    }
    assert stats_data["period"]["label"] == "8w"
    assert stats_data["period"]["basis"] == "closed_at"
    assert stats_data["exclusions"] == {
        "open_positions": 1,
        "partial_trades": 0,
        "unreadable_trades": 0,
        "outside_period": 0,
    }
    assert stats_data["avg_r"] == {"value": None, "samples": 0, "excluded_null_r": 0}
    assert stats_data["totals"] == {
        "gross_pnl_usd": None,
        "fees_usd": None,
        "funding_usd": None,
        "net_pnl_usd": None,
    }
    assert isinstance(stats_data["definitions"], dict)

    assert exported.status_code == 200, exported.text
    export_envelope = exported.json()
    validate("api-envelope.schema.json", export_envelope)
    result = export_envelope["data"]
    assert set(result) == {
        "format",
        "path",
        "rows",
        "bytes",
        "neutralized",
        "skipped_unreadable",
        "data_quality",
    }
    assert result["format"] == "csv"
    assert Path(result["path"]).is_file()
    assert result["rows"] == 0
    assert isinstance(result["bytes"], int) and result["bytes"] > 0
    assert result["neutralized"] == 0
    assert result["skipped_unreadable"] == 0
    assert result["data_quality"] == "MISSING"
    assert export_envelope["meta"]["data_quality"] == "MISSING"


def test_the_journal_compare_envelope_validates(tmp_path: Path) -> None:
    loop = _helpers.build_loop(tmp_path)
    _position, trade = _close_position(loop)
    client, auth = _app(loop)

    with client:
        response = client.get(f"/api/v1/journal/compare/{trade['trade_id']}", headers=auth)

    assert response.status_code == 200, response.text
    envelope = response.json()
    validate("api-envelope.schema.json", envelope)
    assert envelope["meta"]["provenance"] == "MIXED"
    data = envelope["data"]
    assert data["trade_id"] == trade["trade_id"]
    assert set(data) == {
        "trade_id",
        "position_id",
        "symbol",
        "side",
        "opened_at",
        "closed_at",
        "net_pnl_usd",
        "zone",
        "slippage",
        "first_touch",
        "decision",
        "reasons",
        "data_quality",
    }
    assert set(data["zone"]) == {
        "status",
        "entry",
        "zone_low",
        "zone_high",
        "inside",
        "distance_bps",
        "reasons",
    }
    assert set(data["slippage"]) == {
        "status",
        "entry_avg_price",
        "reference_price",
        "reference_as_of",
        "bps",
        "formula",
        "reasons",
    }
    assert set(data["first_touch"]) == {"kind", "at", "ambiguous", "status", "reasons"}
    assert set(data["decision"]) == {
        "status",
        "decision",
        "direction_agrees",
        "matched",
        "net_pnl_usd",
        "reasons",
        "decision_id",
    }
    assert data["first_touch"]["status"] == "N/A"
    assert data["zone"]["status"] in {"INSIDE", "ABOVE_ZONE", "BELOW_ZONE"}
    assert isinstance(data["zone"]["reasons"], list)
    assert isinstance(data["slippage"]["formula"], str)
    assert isinstance(data["reasons"], list)


def test_a_huge_but_shape_valid_period_is_a_catalog_validation_error(
    tmp_path: Path,
) -> None:
    loop = _helpers.build_loop(tmp_path)
    client, auth = _app(loop)

    with client:
        stats = client.get("/api/v1/journal/stats?period=999999d", headers=auth)
        export = client.get("/api/v1/journal/export?period=999999999999999m", headers=auth)

    assert stats.status_code == 422, stats.text
    assert stats.json()["error"]["code"] == "VALIDATION_FAILED"
    assert export.status_code == 422, export.text
    assert export.json()["error"]["code"] == "VALIDATION_FAILED"


def test_an_invalid_period_and_format_are_catalog_validation_errors(
    tmp_path: Path,
) -> None:
    loop = _helpers.build_loop(tmp_path)
    client, auth = _app(loop)

    with client:
        bad_period = client.get("/api/v1/journal/stats?period=8x", headers=auth)
        bad_format = client.get("/api/v1/journal/export?format=parquet", headers=auth)

    assert bad_period.status_code == 422, bad_period.text
    assert bad_period.json()["error"]["code"] == "VALIDATION_FAILED"
    assert bad_format.status_code == 422, bad_format.text
    assert bad_format.json()["error"]["code"] == "VALIDATION_FAILED"
