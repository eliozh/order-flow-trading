"""Budget threshold API: projection, last alert, validation, and idempotency (F10e)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import AuditEventDraft, BudgetRecord, LLMCallRecord

NOW = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings, clock=FixedClock(NOW))
    client = TestClient(app)
    return client, {"Authorization": f"Bearer {app.state.api_token}"}


def _put(
    client: TestClient,
    auth: dict[str, str],
    payload: dict[str, Any],
    *,
    key: str | None = "budget-key-1",
) -> Any:
    headers = dict(auth)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.put("/api/v1/budget", json=payload, headers=headers)


def _month_record(store: Any, *, index: int, cost_usd: str) -> None:
    store.save_llm_call(
        LLMCallRecord(
            call_id=f"CALL-{index}",
            decision_id=None,
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
        )
    )
    store.save_budget_record(
        BudgetRecord(
            record_id=f"BUD-{index}",
            call_id=f"CALL-{index}",
            decision_id=None,
            occurred_at=utc_timestamp(NOW),
            provider="openai",
            model="gpt-fixture",
            cost_usd=cost_usd,
            price_version="2026-11-09",
            unpriced=False,
            budget_state="OK",
            period_day="2026-11-09",
            period_week="2026-W46",
            period_month="2026-11",
            role="ANALYSIS",
            payload_json="{}",
        )
    )


def _threshold_audits(client: TestClient) -> list[dict[str, Any]]:
    return [
        json.loads(event.payload_json)
        for event in client.app.state.container.store.list_audit_events()
        if event.event_type == "BUDGET_THRESHOLDS_UPDATED"
    ]


def _expected_projection(spend: str) -> str:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    fraction = Decimal(str((NOW - start).total_seconds())) / Decimal(
        str((end - start).total_seconds())
    )
    quantum = Decimal("0.00000001")
    return format((Decimal(spend) / fraction).quantize(quantum, rounding=ROUND_HALF_UP), "f")


def test_budget_get_reports_zero_projection_and_no_alert_when_empty(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["projected_month_end_usd"] == "0.00000000"
    assert data["projection_basis"]["month_spend_usd"] == "0.00000000"
    assert data["projection_basis"]["as_of"] == utc_timestamp(NOW)
    assert data["last_alert"] is None
    assert data["usage_quality"] == "COMPLETE"
    # The existing contract is unchanged.
    assert data["thresholds"]["daily_usd"] == "10.00"
    assert set(data["periods"]) == {"day", "week", "month"}


def test_budget_get_projects_month_end_from_real_spend(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        _month_record(client.app.state.container.store, index=1, cost_usd="12.50000000")
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["projection_basis"]["month_spend_usd"] == "12.50000000"
    assert data["projected_month_end_usd"] == _expected_projection("12.50000000")
    assert data["periods"]["month"]["spend_usd"] == "12.50000000"


def test_budget_get_exposes_the_last_threshold_alert(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        client.app.state.container.store.append_audit_event(
            AuditEventDraft(
                event_type="BUDGET_THRESHOLD",
                actor={"kind": "SYSTEM", "id": "budget_service"},
                subject={"kind": "BUDGET", "id": "BUD-1"},
                payload={
                    "period": "day",
                    "state": "WARN",
                    "spend_usd": "8.25000000",
                    "threshold_usd": "10.00",
                    "provider": "openai",
                    "model": "gpt-fixture",
                },
                created_at=utc_timestamp(NOW),
            )
        )
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["last_alert"] == {
        "period": "day",
        "state": "WARN",
        "spend_usd": "8.25000000",
        "threshold_usd": "10.00",
        "occurred_at": utc_timestamp(NOW),
        "provider": "openai",
        "model": "gpt-fixture",
    }


def test_budget_get_returns_null_for_an_incomplete_alert_payload(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        client.app.state.container.store.append_audit_event(
            AuditEventDraft(
                event_type="BUDGET_THRESHOLD",
                actor={"kind": "SYSTEM", "id": "budget_service"},
                subject={"kind": "BUDGET", "id": "BUD-1"},
                payload={"period": "day"},
                created_at=utc_timestamp(NOW),
            )
        )
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["last_alert"] is None


def test_budget_put_writes_one_web_audit_and_returns_changed(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(
            client,
            auth,
            {"daily_usd": "20.00", "weekly_usd": "60.00", "monthly_usd": "200.00"},
        )
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["changed"] is True
    assert data["thresholds"]["daily_usd"] == "20.00"
    assert data["thresholds"]["weekly_usd"] == "60.00"
    assert data["thresholds"]["monthly_usd"] == "200.00"
    assert len(audits) == 1
    body = audits[0]
    assert body["actor"] == {"kind": "USER", "id": "web"}
    assert body["subject"] == {"kind": "BUDGET_SETTINGS", "id": "default"}
    assert body["payload"]["before"]["daily_usd"] == "10.00"
    assert body["payload"]["after"]["daily_usd"] == "20.00"


def test_budget_put_replays_an_identical_request_without_writing(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        first = _put(client, auth, {"daily_usd": "20.00"})
        replay = _put(client, auth, {"daily_usd": "20.00"})
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert replay.json()["data"] == first.json()["data"]
    assert len(audits) == 1


def test_budget_put_conflicts_on_the_same_key_with_a_different_payload(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        _put(client, auth, {"daily_usd": "20.00"})
        conflict = _put(client, auth, {"daily_usd": "21.00"})
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(audits) == 1


def test_budget_put_no_op_with_a_new_key_writes_no_audit(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(
            client,
            auth,
            {"daily_usd": "10.00", "weekly_usd": "50.00", "monthly_usd": "150.00"},
            key="fresh-no-op",
        )
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["changed"] is False
    assert audits == []


def test_budget_put_without_an_idempotency_key_succeeds_and_audits(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(client, auth, {"daily_usd": "20.00"}, key=None)
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["changed"] is True
    assert response.json()["data"]["thresholds"]["daily_usd"] == "20.00"
    assert response.json()["meta"]["data_quality"] == "COMPLETE"
    assert len(audits) == 1


def test_budget_put_without_a_key_has_no_replay_meta(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        first = _put(client, auth, {"daily_usd": "20.00"}, key=None)
        second = _put(client, auth, {"daily_usd": "21.00"}, key=None)
    finally:
        client.close()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["meta"]["idempotent_replay"] is False
    assert second.json()["meta"]["idempotent_replay"] is False
    assert second.json()["data"]["changed"] is True


def test_budget_put_returns_the_authoritative_snapshot(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(client, auth, {"daily_usd": "20.00"})
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["changed"] is True
    assert data["usage_quality"] == "COMPLETE"
    assert data["thresholds"]["daily_usd"] == "20.00"
    assert data["budget_state"] == "OK"
    assert set(data["periods"]) == {"day", "week", "month"}
    assert data["projected_month_end_usd"] == "0.00000000"
    assert data["projection_basis"]["month_spend_usd"] == "0.00000000"
    assert data["last_alert"] is None


def test_budget_put_treats_reformatted_values_as_unchanged(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        seed = _put(client, auth, {"warn_pct": 75}, key="seed-warn")
        assert seed.status_code == 200, seed.text
        stored_before = client.app.state.container.store.get_budget_settings()
        audits_before = len(_threshold_audits(client))
        responses = [
            _put(client, auth, {"daily_usd": value}, key=f"fmt-{value}")
            for value in ("10", "10.0", "10.000")
        ]
        audits = _threshold_audits(client)
        stored = client.app.state.container.store.get_budget_settings()
    finally:
        client.close()

    assert stored_before is not None
    assert audits_before == 1
    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["changed"] is False
    assert len(audits) == audits_before
    assert stored is not None
    assert stored.daily_usd == "10.00"
    assert stored.warn_pct == 75.0
    # A no-op must not even rewrite the row (the timestamp stays the seed's).
    assert stored.updated_at == stored_before.updated_at


def test_budget_put_reports_every_invalid_money_field_in_one_pass(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(
            client,
            auth,
            {"daily_usd": "0", "weekly_usd": "-1", "warn_pct": 0},
            key="multi-invalid",
        )
        audits = _threshold_audits(client)
        # The failed build rolled the transaction back with no settings row.
        stored = client.app.state.container.store.get_budget_settings()
    finally:
        client.close()

    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert {(detail["field"], detail["code"]) for detail in details} == {
        ("daily_usd", "not_positive"),
        ("weekly_usd", "not_positive"),
        ("warn_pct", "out_of_range"),
    }
    assert audits == []
    assert stored is None


def test_budget_put_releases_the_key_after_a_validation_error(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        refused = _put(client, auth, {"weekly_usd": "5.00"}, key="retry-key")
        corrected = _put(
            client,
            auth,
            {"daily_usd": "10.00", "weekly_usd": "60.00", "monthly_usd": "150.00"},
            key="retry-key",
        )
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert refused.status_code == 422, refused.text
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["changed"] is True
    assert len(audits) == 1


def test_budget_get_degrades_usage_but_keeps_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, auth = _client(tmp_path)
    store = client.app.state.container.store

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(store, "list_budget_records_for_period", _unavailable)
    try:
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["usage_quality"] == "UNAVAILABLE"
    assert response.json()["meta"]["data_quality"] == "MISSING"
    assert data["thresholds"]["daily_usd"] == "10.00"
    assert data["budget_state"] is None
    assert data["periods"] is None
    assert data["projected_month_end_usd"] is None
    assert data["projection_basis"] is None
    assert data["last_alert"] is None


def test_budget_put_keeps_a_committed_save_when_usage_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, auth = _client(tmp_path)
    store = client.app.state.container.store

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(store, "list_budget_records_for_period", _unavailable)
    try:
        response = _put(client, auth, {"daily_usd": "20.00"})
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["changed"] is True
    assert data["usage_quality"] == "UNAVAILABLE"
    assert response.json()["meta"]["data_quality"] == "MISSING"
    assert data["thresholds"]["daily_usd"] == "20.00"
    assert data["periods"] is None
    assert len(audits) == 1


def test_budget_put_keyed_normal_envelope_is_complete(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(client, auth, {"daily_usd": "20.00"})
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["meta"]["data_quality"] == "COMPLETE"
    assert response.json()["data"]["usage_quality"] == "COMPLETE"


def test_budget_put_keyed_degraded_envelope_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, auth = _client(tmp_path)
    store = client.app.state.container.store

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(store, "list_budget_records_for_period", _unavailable)
    try:
        response = _put(client, auth, {"daily_usd": "20.00"})
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["usage_quality"] == "UNAVAILABLE"
    assert response.json()["meta"]["data_quality"] == "MISSING"


def test_budget_get_returns_503_when_thresholds_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, auth = _client(tmp_path)
    store = client.app.state.container.store

    def _unavailable() -> Any:
        raise sqlite3.OperationalError("budget settings unreadable")

    monkeypatch.setattr(store, "get_budget_settings", _unavailable)
    try:
        response = client.get("/api/v1/budget", headers=auth)
    finally:
        client.close()

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


def test_budget_put_reports_ordering_details_on_the_lower_bound_field(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        weekly = _put(client, auth, {"daily_usd": "10.00", "weekly_usd": "5.00"}, key="order-1")
        monthly = _put(client, auth, {"weekly_usd": "50.00", "monthly_usd": "20.00"}, key="order-2")
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert weekly.status_code == 422, weekly.text
    assert weekly.json()["error"]["code"] == "VALIDATION_FAILED"
    assert weekly.json()["error"]["details"] == [
        {
            "field": "weekly_usd",
            "code": "ordering",
            "message": "weekly_usd must be greater than or equal to daily_usd",
        }
    ]
    assert monthly.status_code == 422, monthly.text
    assert monthly.json()["error"]["details"] == [
        {
            "field": "monthly_usd",
            "code": "ordering",
            "message": "monthly_usd must be greater than or equal to weekly_usd",
        }
    ]
    assert audits == []


def test_budget_put_reports_positivity_and_warn_pct_details(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        negative = _put(client, auth, {"daily_usd": "0"}, key="bad-daily")
        warn = _put(client, auth, {"warn_pct": 0}, key="bad-warn")
        audits = _threshold_audits(client)
    finally:
        client.close()

    assert negative.status_code == 422, negative.text
    assert negative.json()["error"]["details"][0]["field"] == "daily_usd"
    assert warn.status_code == 422, warn.text
    assert warn.json()["error"]["details"][0]["field"] == "warn_pct"
    assert audits == []


def test_budget_put_allows_equal_adjacent_thresholds(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    try:
        response = _put(
            client,
            auth,
            {"daily_usd": "50.00", "weekly_usd": "50.00", "monthly_usd": "50.00"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["changed"] is True
