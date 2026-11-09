"""Every opportunity fixture materialises a contract-valid condition ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import _helpers
import pytest
from _registry import validate

from oft.application.llm_validation import validate_output
from oft.application.opportunity_service import OpportunityService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.domain.conditions import (
    ConditionType,
    condition_spec,
    contract_required_data,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_ISO = "2026-11-06T12:00:00.000Z"
AVAILABLE = frozenset(
    {
        "MARK_PRICE",
        "CONTRACT_SPECS",
        "METRICS_5M",
        "OPEN_INTEREST",
        "FUNDING",
        "FOOTPRINT",
        "SESSION",
    }
)
CASES = (
    "opportunity_price.json",
    "opportunity_delta_cvd.json",
    "opportunity_oi_funding.json",
    "opportunity_footprint.json",
    "opportunity_time_session.json",
)


def _provenance() -> dict[str, object]:
    return {
        "llm": {
            "provider": "stub",
            "model": "fixture",
            "model_snapshot": None,
            "prompt_id": "analysis",
            "prompt_version": 1,
            "prompt_hash": "a" * 64,
            "knowledge_artifact_hash": None,
            "input_hash": "b" * 64,
            "schema_version": "1.0.0",
            "temperature": 0.0,
            "seed": None,
            "request_id": "REQ-1",
            "latency_ms": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
            "cost": {
                "amount_usd": "0",
                "currency": "USD",
                "price_version": "stub/v1",
                "unpriced": True,
            },
            "recorded_at": NOW_ISO,
        },
        "calc_snapshot_id": "SNAP-1",
        "calc_snapshot_hash": "c" * 64,
        "calc_metric_version": "v1",
    }


def _data_quality() -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "reasons": [],
        "gaps": [],
        "last_event_at": NOW_ISO,
        "coverage_start": None,
    }


def _materialise(tmp_path: Path, name: str) -> dict[str, object]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    outcome = validate_output(text, role="ANALYSIS")
    assert outcome.valid, [issue.message for issue in outcome.errors]
    assert outcome.output is not None
    draft = outcome.output.opportunity
    assert draft is not None

    settings = _helpers.settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    service = OpportunityService(store=container.store, clock=container.clock)
    opportunity = service.create_from_decision(
        opportunity_draft=draft,
        subject={"symbol": "BTCUSDT"},
        decision_id="DEC-1",
        scan_id="SCAN-1",
        report_id="RPT-1",
        confidence=outcome.output.confidence,
        provenance=_provenance(),
        data_quality=_data_quality(),
        at=NOW_ISO,
        available_data=AVAILABLE,
    )
    record = container.store.get_opportunity(opportunity.opportunity_id)
    assert record is not None
    payload: dict[str, object] = json.loads(record.payload_json)
    return payload


@pytest.mark.parametrize("name", CASES)
def test_fixture_opportunity_validates_the_contract(tmp_path: Path, name: str) -> None:
    payload = _materialise(tmp_path, name)

    validate("opportunity.schema.json", payload)

    trigger = payload["trigger_conditions"]
    invalidation = payload["invalidation_conditions"]
    assert isinstance(trigger, list) and isinstance(invalidation, list)
    assert [item["condition_id"] for item in trigger] == [
        f"C-{index}" for index in range(1, len(trigger) + 1)
    ]
    assert [item["condition_id"] for item in invalidation] == [
        f"I-{index}" for index in range(1, len(invalidation) + 1)
    ]
    for item in (*trigger, *invalidation):
        condition_type = ConditionType(item["type"])
        spec = condition_spec(item["type"])
        assert set(spec.required_params) <= set(item["params"])
        assert all(value is not None for value in item["params"].values())
        assert item["required_data"] == list(contract_required_data(condition_type))
        assert item["required_data"]


def test_invalidation_drafts_map_to_real_types(tmp_path: Path) -> None:
    payload = _materialise(tmp_path, "opportunity_price.json")
    invalidation = {item["type"]: item for item in payload["invalidation_conditions"]}

    cross = invalidation["PRICE_CROSS_BELOW"]
    assert cross["params"] == {"price": "69000.00"}
    assert cross["evaluable"] is True

    outside = invalidation["PRICE_OUTSIDE_ZONE"]
    assert outside["params"] == {"price_low": "69000.00", "price_high": "71000.00"}


def test_manual_invalidation_is_code_only_and_never_evaluable(tmp_path: Path) -> None:
    payload = _materialise(tmp_path, "opportunity_time_session.json")
    manual = next(
        item
        for item in payload["invalidation_conditions"]
        if item["params"].get("original_type") == "MANUAL"
    )

    assert manual["type"] == "TIME_WINDOW"
    assert manual["evaluable"] is False
    assert manual["unavailable_reason"] == "manual invalidation is code-only"


def test_time_expired_maps_to_a_time_window(tmp_path: Path) -> None:
    payload = _materialise(tmp_path, "opportunity_time_session.json")
    expired = next(
        item
        for item in payload["invalidation_conditions"]
        if item["type"] == "TIME_WINDOW" and item["params"] == {"original_type": "TIME_EXPIRED"}
    )

    assert expired["window"]["start"] == NOW_ISO
    assert expired["window"]["end"] == "2026-11-06T13:00:00.000Z"
    assert expired["evaluable"] is True


def test_historical_liquidations_are_unevaluable_in_the_contract(tmp_path: Path) -> None:
    payload = _materialise(tmp_path, "opportunity_oi_funding.json")
    liquidation = next(
        item
        for item in payload["trigger_conditions"]
        if item["type"] == "LIQUIDATION_NOTIONAL_ABOVE"
    )

    assert liquidation["evaluable"] is False
    assert "LIQUIDATIONS" in liquidation["unavailable_reason"]


def test_fixtures_cover_every_condition_type(tmp_path: Path) -> None:
    seen: set[str] = set()
    for name in CASES:
        payload = _materialise(tmp_path / name.replace(".json", ""), name)
        for item in (
            *payload["trigger_conditions"],
            *payload["invalidation_conditions"],
        ):
            seen.add(item["type"])

    assert seen == {member.value for member in ConditionType}


def test_persisted_decimals_remain_exact(tmp_path: Path) -> None:
    payload = _materialise(tmp_path, "opportunity_price.json")
    zone_trigger = payload["trigger_conditions"][1]

    assert zone_trigger["params"]["price_low"] == "69900.00"
    assert Decimal(zone_trigger["params"]["price_low"]) == Decimal("69900.00")
    assert isinstance(payload["confidence"], float)
    assert isinstance(payload["zone"]["low"], str)
