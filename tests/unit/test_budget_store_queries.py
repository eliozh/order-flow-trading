"""Budget store queries used by the hot path (M24)."""

from __future__ import annotations

from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.config import Settings
from oft.ports.store import BudgetRecord, LLMCallRecord


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _call(store: SqliteStore, call_id: str, created_at: str) -> None:
    store.save_llm_call(
        LLMCallRecord(
            call_id=call_id,
            role="ANALYSIS",
            provider="openai",
            model="m",
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
            created_at=created_at,
        )
    )


def _budget(call_id: str, occurred_at: str, day: str, week: str, month: str) -> BudgetRecord:
    return BudgetRecord(
        record_id=f"BUD-{call_id}",
        call_id=call_id,
        decision_id=None,
        occurred_at=occurred_at,
        provider="openai",
        model="m",
        cost_usd="1.00000000",
        price_version="v",
        unpriced=False,
        budget_state="OK",
        period_day=day,
        payload_json="{}",
        period_week=week,
        period_month=month,
        role="ANALYSIS",
    )


def test_last_budget_record_returns_the_latest_by_occurred_at_then_rowid(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for call_id, at in (
        ("CALL-A", "2026-11-08T10:00:00.000Z"),
        ("CALL-C", "2026-11-09T10:00:00.000Z"),
        ("CALL-B", "2026-11-09T10:00:00.000Z"),
    ):
        _call(store, call_id, at)
        store.save_budget_record(_budget(call_id, at, "2026-11-09", "2026-W46", "2026-11"))

    last = store.last_budget_record()

    assert last is not None
    # The later rowid wins the occurred_at tie.
    assert last.call_id == "CALL-B"
    assert store.list_budget_records()[0].call_id == "CALL-A"


def test_list_budget_records_for_period_filters_in_sql(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _call(store, "CALL-W45", "2026-11-04T10:00:00.000Z")
    store.save_budget_record(
        _budget("CALL-W45", "2026-11-04T10:00:00.000Z", "2026-11-04", "2026-W45", "2026-11")
    )
    _call(store, "CALL-W46", "2026-11-09T10:00:00.000Z")
    store.save_budget_record(
        _budget("CALL-W46", "2026-11-09T10:00:00.000Z", "2026-11-09", "2026-W46", "2026-11")
    )

    week = store.list_budget_records_for_period("week", "2026-W46")
    month = store.list_budget_records_for_period("month", "2026-11")
    day = store.list_budget_records_for_period("day", "2026-11-04")

    assert [record.call_id for record in week] == ["CALL-W46"]
    assert {record.call_id for record in month} == {"CALL-W45", "CALL-W46"}
    assert [record.call_id for record in day] == ["CALL-W45"]
