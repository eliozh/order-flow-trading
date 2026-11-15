"""Month-end projection and last-alert parsing for the budget service (F10e)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_read_model import projection_for, state_for
from oft.application.budget_service import BudgetService
from oft.config import Settings
from oft.ports.store import AuditEventRecord, BudgetRecord, LLMCallRecord

MONTH_START = "2026-11-01T00:00:00.000Z"
NOW = "2026-11-16T00:00:00.000Z"
OCCURRED_AT = "2026-11-10T00:00:00.000Z"


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _save_month_record(store: SqliteStore, *, index: int, cost_usd: str) -> None:
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
            created_at=OCCURRED_AT,
        )
    )
    store.save_budget_record(
        BudgetRecord(
            record_id=f"BUD-{index}",
            call_id=f"CALL-{index}",
            decision_id=None,
            occurred_at=OCCURRED_AT,
            provider="openai",
            model="gpt-fixture",
            cost_usd=cost_usd,
            price_version="2026-11-09",
            unpriced=False,
            budget_state="OK",
            period_day="2026-11-10",
            period_week="2026-W45",
            period_month="2026-11",
            role="ANALYSIS",
            payload_json="{}",
        )
    )


def _audit(
    *,
    payload_json: str = '{"actor": {}, "subject": {}, "payload": {}}',
) -> AuditEventRecord:
    return AuditEventRecord(
        event_id="AUD-00000001-AAAAAAAA",
        seq=1,
        event_type="BUDGET_THRESHOLD",
        created_at="2026-11-10T00:00:00.000Z",
        previous_hash="0" * 64,
        event_hash="1" * 64,
        payload_json=payload_json,
    )


class _FakeAuditStore:
    def __init__(self, record: AuditEventRecord | None) -> None:
        self._record = record

    def last_audit_event_of_type(self, event_type: str) -> AuditEventRecord | None:
        assert event_type == "BUDGET_THRESHOLD"
        return self._record


def test_projection_divides_month_spend_by_elapsed_fraction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_month_record(store, index=1, cost_usd="10.00")
    service = BudgetService(store=store)

    projection = service.projection(NOW)

    # 15 of 30 elapsed days -> exactly half the month.
    assert projection["projected_month_end_usd"] == "20.00000000"
    basis = projection["projection_basis"]
    assert basis == {
        "month_spend_usd": "10.00000000",
        "elapsed_fraction": pytest.approx(0.5),
        "as_of": NOW,
    }


def test_projection_of_zero_spend_is_zero_not_a_division_error(tmp_path: Path) -> None:
    service = BudgetService(store=_store(tmp_path))

    projection = service.projection(NOW)

    assert projection["projected_month_end_usd"] == "0.00000000"
    assert projection["projection_basis"]["month_spend_usd"] == "0.00000000"
    assert projection["projection_basis"]["elapsed_fraction"] == pytest.approx(0.5)


def test_projection_clamps_the_elapsed_fraction_at_one_second(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save_month_record(store, index=1, cost_usd="1.00")
    service = BudgetService(store=store)

    projection = service.projection(MONTH_START)

    fraction = projection["projection_basis"]["elapsed_fraction"]
    month_seconds = Decimal(30 * 24 * 60 * 60)
    expected_fraction = float(Decimal("1") / month_seconds)
    assert fraction == pytest.approx(expected_fraction)
    assert fraction > 0
    # The projection is finite and money-shaped rather than an overflow/NaN.
    assert Decimal(str(projection["projected_month_end_usd"])) > 0


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        # Last second of a 30-day month: the fraction approaches (but never
        # reaches) one, so the projection is only slightly above the spend.
        ("2026-11-30T23:59:59.000Z", "10.00000386"),
        # December rollover exercises the year+1 / month=1 branch (31 days).
        ("2026-12-31T23:59:59.000Z", "10.00000373"),
        # Leap February (29 days) around midday.
        ("2028-02-29T12:00:00.000Z", "10.17543860"),
    ],
)
def test_projection_handles_month_end_rollover_and_leap_february(at: str, expected: str) -> None:
    projection = projection_for(at, Decimal("10"))

    assert projection["projected_month_end_usd"] == expected
    fraction = projection["projection_basis"]["elapsed_fraction"]
    assert 0 < fraction <= 1


def test_state_for_warn_boundary_uses_exact_decimal_math() -> None:
    # 0.1 * 30 / 100 is exactly 0.03 in Decimal but 0.030000000000000002 in
    # binary floats; the server stays authoritative at the warn boundary.
    assert state_for(Decimal("0.03"), Decimal("0.1"), 30.0) == "WARN"


def test_snapshot_computes_each_window_once_and_reads_thresholds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _save_month_record(store, index=1, cost_usd="10.00")
    windows: list[str] = []
    original_records = store.list_budget_records_for_period

    def counting_records(period: str, key: str) -> Any:
        windows.append(period)
        return original_records(period, key)

    threshold_reads: list[int] = []
    original_settings = store.get_budget_settings

    def counting_settings() -> Any:
        threshold_reads.append(1)
        return original_settings()

    monkeypatch.setattr(store, "list_budget_records_for_period", counting_records)
    monkeypatch.setattr(store, "get_budget_settings", counting_settings)

    snapshot = BudgetService(store=store).snapshot(NOW)

    # The month window feeds the projection instead of being scanned twice.
    assert sorted(windows) == ["day", "month", "week"]
    assert len(threshold_reads) == 1
    assert snapshot["usage_quality"] == "COMPLETE"
    assert snapshot["projected_month_end_usd"] == "20.00000000"


def test_last_alert_is_null_when_the_store_has_no_threshold_event() -> None:
    service = BudgetService(store=_FakeAuditStore(None))

    assert service.last_alert() is None


@pytest.mark.parametrize(
    "payload_json",
    [
        "not-json",
        "[]",
        '{"actor": {}, "subject": {}}',
        '{"payload": {"period": "day"}}',
        '{"payload": {"period": "year", "state": "WARN", "spend_usd": "1", '
        '"threshold_usd": "1", "provider": "openai", "model": "m"}}',
        # Numeric content must parse as a finite decimal; malformed money is
        # rejected rather than relayed to the Web/CLI verbatim.
        '{"payload": {"period": "day", "state": "WARN", "spend_usd": "abc", '
        '"threshold_usd": "10.00", "provider": "openai", "model": "m"}}',
        '{"payload": {"period": "day", "state": "WARN", "spend_usd": "NaN", '
        '"threshold_usd": "10.00", "provider": "openai", "model": "m"}}',
        '{"payload": {"period": "day", "state": "WARN", "spend_usd": "", '
        '"threshold_usd": "10.00", "provider": "openai", "model": "m"}}',
        '{"payload": {"period": "day", "state": "WARN", "spend_usd": "8.25", '
        '"threshold_usd": "Infinity", "provider": "openai", "model": "m"}}',
    ],
)
def test_last_alert_returns_null_for_a_malformed_payload(payload_json: str) -> None:
    service = BudgetService(store=_FakeAuditStore(_audit(payload_json=payload_json)))

    assert service.last_alert() is None


def test_last_alert_returns_the_crossing_envelope_and_audit_time() -> None:
    body: dict[str, Any] = {
        "payload": {
            "period": "day",
            "state": "WARN",
            "spend_usd": "8.25000000",
            "threshold_usd": "10.00",
            "provider": "openai",
            "model": "gpt-fixture",
        }
    }
    import json

    service = BudgetService(store=_FakeAuditStore(_audit(payload_json=json.dumps(body))))

    assert service.last_alert() == {
        "period": "day",
        "state": "WARN",
        "spend_usd": "8.25000000",
        "threshold_usd": "10.00",
        "occurred_at": "2026-11-10T00:00:00.000Z",
        "provider": "openai",
        "model": "gpt-fixture",
    }
