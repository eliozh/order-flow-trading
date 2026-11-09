"""LLM usage and budget visibility over a ledger that actually contains spend (T1/T3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application.budget_service import BudgetService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.pricing import ModelPrice, PriceTable
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMResult, LLMUsage
from oft.ports.store import LLMCallRecord

NOW = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _table() -> PriceTable:
    return PriceTable(
        version="2026-11-09",
        models={
            ("openai", "gpt-fixture"): ModelPrice(
                provider="openai",
                model="gpt-fixture",
                input_per_mtok=Decimal("2.50"),
                output_per_mtok=Decimal("10.00"),
                cache_read_per_mtok=Decimal("1.00"),
                cache_write_per_mtok=Decimal("1.00"),
                source_url="https://example.invalid",
                as_of="2026-11-09",
            )
        },
    )


def _result() -> LLMResult:
    return LLMResult(
        provider="openai",
        model="gpt-fixture",
        raw_text="{}",
        usage=LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000),
    )


def _call_row(call_id: str) -> LLMCallRecord:
    return LLMCallRecord(
        call_id=call_id,
        role="ANALYSIS",
        provider="openai",
        model="gpt-fixture",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
        input_hash="ih",
        attempt=1,
        request_id="req",
        status="OK",
        raw_output_hash="rh",
        usage_json="{}",
        latency_ms=1,
        created_at=utc_timestamp(NOW),
        decision_id="DEC-VIS",
    )


def _client(tmp_path: Path) -> tuple[TestClient, Settings, dict[str, str]]:
    settings = _settings(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}"}
    return client, settings, auth


def test_llm_usage_route_is_empty_then_reports_day_week_and_month(tmp_path: Path) -> None:
    client, _settings_, auth = _client(tmp_path)
    try:
        empty = client.get("/api/v1/llm/usage", headers=auth)
        assert empty.status_code == 200, empty.text
        empty_data = empty.json()["data"]
        assert empty_data["call_count"] == 0
        assert empty_data["spend_usd"] == "0.00000000"
        assert empty_data["period"] == "day"

        service = BudgetService(store=client.app.state.container.store, price_table=_table())
        service.record_billed_call(
            call=_call_row("CALL-USAGE"),
            result=_result(),
            occurred_at=utc_timestamp(NOW),
        )

        responses = {
            "day": client.get("/api/v1/llm/usage?period=day", headers=auth),
            "week": client.get("/api/v1/llm/usage?period=week", headers=auth),
            "month": client.get("/api/v1/llm/usage?period=month", headers=auth),
        }
    finally:
        client.close()

    expected_keys = {"day": "2026-11-09", "week": "2026-W46", "month": "2026-11"}
    for period, response in responses.items():
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["period"] == period
        assert data["period_key"] == expected_keys[period]
        assert data["spend_usd"] == "12.50000000"
        assert data["call_count"] == 1
        assert data["unpriced_count"] == 0


def test_budget_route_exposes_state_and_periods_for_real_spend(tmp_path: Path) -> None:
    client, _settings_, auth = _client(tmp_path)
    try:
        service = BudgetService(store=client.app.state.container.store, price_table=_table())
        service.set_thresholds(daily_usd="15.00", updated_at=utc_timestamp(NOW))
        service.record_billed_call(
            call=_call_row("CALL-BUDGET"),
            result=_result(),
            occurred_at=utc_timestamp(NOW),
        )

        shown = client.get("/api/v1/budget", headers=auth)
        usage = client.get("/api/v1/budget/usage?period=day", headers=auth)
    finally:
        client.close()

    assert shown.status_code == 200, shown.text
    data = shown.json()["data"]
    assert data["budget_state"] == "WARN"
    assert data["periods"]["day"]["spend_usd"] == "12.50000000"
    assert data["periods"]["day"]["call_count"] == 1
    assert data["periods"]["day"]["budget_state"] == "WARN"
    assert data["periods"]["week"]["budget_state"] == "OK"
    assert data["periods"]["month"]["budget_state"] == "OK"

    assert usage.status_code == 200, usage.text
    assert usage.json()["data"]["spend_usd"] == "12.50000000"
    assert usage.json()["data"]["budget_state"] == "WARN"

    audits = [
        json.loads(event.payload_json)["payload"]
        for event in client.app.state.container.store.list_audit_events()
        if event.event_type == "BUDGET_THRESHOLD"
    ]
    assert len(audits) == 1
    assert audits[0]["period"] == "day"
    assert audits[0]["state"] == "WARN"
