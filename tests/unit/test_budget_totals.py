"""``budget_totals`` sums money exactly, never through SQLite REAL floats (F9)."""

from __future__ import annotations

from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.config import Settings
from oft.ports.store import BudgetRecord, LLMCallRecord

OCCURRED_AT = "2026-11-09T12:00:00.000Z"
DAY = "2026-11-09"
WEEK = "2026-W45"
MONTH = "2026-11"


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _record_call(store: SqliteStore, index: int, *, cost_usd: str, unpriced: bool) -> None:
    call_id = f"CALL-{index}"
    store.save_llm_call(
        LLMCallRecord(
            call_id=call_id,
            decision_id=None,
            role="ENTRY_RECHECK",
            provider="openai",
            model="gpt-fixture",
            prompt_id="entry_recheck",
            prompt_version=1,
            prompt_hash="prompt-hash",
            input_hash="input-hash",
            attempt=1,
            request_id=f"REQ-{index}",
            status="OK",
            raw_output_hash="raw-hash",
            usage_json="{}",
            latency_ms=1,
            created_at=OCCURRED_AT,
        )
    )
    store.save_budget_record(
        BudgetRecord(
            record_id=f"BUD-{index}",
            call_id=call_id,
            decision_id=None,
            occurred_at=OCCURRED_AT,
            provider="openai",
            model="gpt-fixture",
            cost_usd=cost_usd,
            price_version="2026-11-09",
            unpriced=unpriced,
            budget_state="OK",
            period_day=DAY,
            period_week=WEEK,
            period_month=MONTH,
            role="ENTRY_RECHECK",
            payload_json="{}",
        )
    )


def test_budget_totals_is_exact_beyond_float_precision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # SQLite REAL arithmetic loses the last unit here (…97 versus …98).
    _record_call(store, 1, cost_usd="99999999.99999999", unpriced=False)
    _record_call(store, 2, cost_usd="99999999.99999999", unpriced=False)

    totals = store.budget_totals(day=DAY, week=WEEK, month=MONTH)

    for period in ("day", "week", "month"):
        assert totals[period]["spend_usd"] == "199999999.99999998", period
        assert totals[period]["call_count"] == 2, period
        assert totals[period]["unpriced_count"] == 0, period


def test_budget_totals_counts_unpriced_calls_and_small_costs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(1, 101):
        _record_call(store, index, cost_usd="0.00000001", unpriced=index == 100)

    totals = store.budget_totals(day=DAY, week=WEEK, month=MONTH)

    assert totals["day"]["spend_usd"] == "0.00000100"
    assert totals["day"]["call_count"] == 100
    assert totals["day"]["unpriced_count"] == 1
