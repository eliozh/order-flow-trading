"""Per-call budget ledger, day/week/month states, and threshold audit (D6)."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_service import BudgetConfigError, BudgetService
from oft.domain.pricing import ModelPrice, PriceTable
from oft.ports.llm import LLMResult, LLMUsage
from oft.ports.store import BudgetRecord, LLMCallRecord

NOW = "2026-11-09T10:00:00.000Z"


def _store(tmp_path: Path) -> SqliteStore:
    from oft.config import Settings

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def _call(store: SqliteStore, call_id: str) -> None:
    store.save_llm_call(
        LLMCallRecord(
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
            created_at=NOW,
        )
    )


def _result() -> LLMResult:
    return LLMResult(
        provider="openai",
        model="gpt-fixture",
        raw_text="{}",
        usage=LLMUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
        ),
    )


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


def _record(store: SqliteStore, service: BudgetService, call_id: str):
    _call(store, call_id)
    return service.record_call(
        call_id=call_id,
        decision_id="DEC-1",
        role="ANALYSIS",
        result=_result(),
        occurred_at=NOW,
    )


def test_empty_price_table_is_unpriced_and_periods_are_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store)

    entry = _record(store, service, "CALL-1")

    payload = entry.payload
    assert payload["unpriced"] is True
    assert payload["cost_usd"] == "0"
    assert payload["price_version"] == "none/v1"
    assert payload["period"] == {
        "day": "2026-11-09",
        "week": "2026-W46",
        "month": "2026-11",
    }
    assert entry.record.period_week == "2026-W46"
    assert entry.record.period_month == "2026-11"
    assert entry.record.role == "ANALYSIS"


def test_priced_call_records_unit_prices_and_exact_cost(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())

    entry = _record(store, service, "CALL-2")

    assert entry.payload["unpriced"] is False
    assert entry.payload["cost_usd"] == "12.50000000"
    assert entry.payload["unit_prices"]["input_per_mtok"] == "2.50"
    assert entry.payload["unit_prices"]["output_per_mtok"] == "10.00"
    assert entry.payload["price_version"] == "2026-11-09"


def test_default_thresholds_match_adr_0005(tmp_path: Path) -> None:
    thresholds = BudgetService(store=_store(tmp_path)).thresholds()

    assert thresholds.daily_usd == Decimal("10.00")
    assert thresholds.weekly_usd == Decimal("50.00")
    assert thresholds.monthly_usd == Decimal("150.00")
    assert thresholds.warn_pct == 80.0


def test_exceeding_a_threshold_alerts_but_never_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="1.00", updated_at=NOW)

    entry = _record(store, service, "CALL-3")

    assert entry.payload["budget_state"] == "EXCEEDED"
    events = [
        event for event in store.list_audit_events() if event.event_type == "BUDGET_THRESHOLD"
    ]
    assert len(events) == 1
    assert "12.50000000" in events[0].payload_json


def test_warn_state_at_the_configured_percentage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", warn_pct=80.0, updated_at=NOW)

    entry = _record(store, service, "CALL-4")

    # 12.50 / 15.00 = 83.3% >= warn 80%, below 100%.
    assert entry.payload["budget_state"] == "WARN"


def test_a_rank_increase_logs_once_and_repeats_do_not(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)

    first = _record(store, service, "CALL-5")
    second = _record(store, service, "CALL-6")

    assert first.payload["budget_state"] == "WARN"
    assert second.payload["budget_state"] == "EXCEEDED"
    events = [
        event for event in store.list_audit_events() if event.event_type == "BUDGET_THRESHOLD"
    ]
    assert len(events) == 2


def test_usage_reports_spend_calls_unpriced_thresholds_and_breakdowns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    _record(store, service, "CALL-7")

    usage = service.usage("day", at=NOW)

    assert usage["period"] == "day"
    assert usage["period_key"] == "2026-11-09"
    assert usage["spend_usd"] == "12.50000000"
    assert usage["call_count"] == 1
    assert usage["unpriced_count"] == 0
    assert usage["thresholds"]["daily_usd"] == "10.00"
    assert usage["budget_state"] == "EXCEEDED"
    assert usage["by_provider"] == [
        {
            "provider": "openai",
            "spend_usd": "12.50000000",
            "call_count": 1,
            "unpriced_count": 0,
        }
    ]
    assert usage["by_model"][0]["model"] == "gpt-fixture"


def test_set_thresholds_persists_and_audits_before_and_after(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store)

    updated = service.set_thresholds(weekly_usd="25.00", warn_pct=70.0, updated_at=NOW)

    assert updated.weekly_usd == Decimal("25.00")
    assert updated.warn_pct == 70.0
    assert BudgetService(store=store).thresholds().weekly_usd == Decimal("25.00")
    events = [e for e in store.list_audit_events() if e.event_type == "BUDGET_THRESHOLDS_UPDATED"]
    assert len(events) == 1
    payload = events[0].payload_json
    assert "25.00" in payload
    assert "50.00" in payload


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_usd": "0"},
        {"daily_usd": "-1.00"},
        {"daily_usd": "not-a-decimal"},
        {"warn_pct": 0},
        {"warn_pct": 101},
    ],
)
def test_invalid_threshold_values_are_rejected(tmp_path: Path, kwargs: dict) -> None:
    service = BudgetService(store=_store(tmp_path))

    with pytest.raises(BudgetConfigError):
        service.set_thresholds(updated_at=NOW, **kwargs)


def test_unknown_usage_period_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BudgetConfigError):
        BudgetService(store=_store(tmp_path)).usage("year", at=NOW)


def _record_at(store, service, call_id: str, occurred_at: str):
    _call(store, call_id)
    return service.record_call(
        call_id=call_id,
        decision_id="DEC-TIME",
        role="ANALYSIS",
        result=_result(),
        occurred_at=occurred_at,
    )


def test_a_period_rollover_still_alerts_when_the_previous_row_was_exceeded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="1.00", updated_at=NOW)
    _record_at(store, service, "CALL-D1", "2026-11-08T10:00:00.000Z")
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)

    day_two = _record_at(store, service, "CALL-D2", "2026-11-09T10:00:00.000Z")

    assert day_two.payload["budget_state"] == "WARN"
    events = [
        event for event in store.list_audit_events() if event.event_type == "BUDGET_THRESHOLD"
    ]
    # One for the first day's EXCEEDED, one for the new day's WARN crossing.
    assert len(events) == 2
    assert '"period": "day"' in events[-1].payload_json


def test_duplicate_call_id_is_an_idempotent_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)

    _record(store, service, "CALL-DUP")
    service.record_call(
        call_id="CALL-DUP",
        decision_id="DEC-1",
        role="ANALYSIS",
        result=_result(),
        occurred_at=NOW,
    )

    assert len(store.list_budget_records()) == 1
    events = [
        event for event in store.list_audit_events() if event.event_type == "BUDGET_THRESHOLD"
    ]
    assert len(events) == 1
    assert service.usage("day", at=NOW)["spend_usd"] == "12.50000000"


def test_concurrent_record_calls_do_not_lose_spend(tmp_path: Path) -> None:
    import threading

    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="1000.00", updated_at=NOW)
    errors: list[BaseException] = []

    def record(index: int) -> None:
        try:
            _record(store, service, f"CALL-CONC-{index}")
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    usage = service.usage("day", at=NOW)
    assert usage["call_count"] == 8
    assert usage["spend_usd"] == "100.00000000"


def test_reinvoking_one_decision_records_two_billed_calls(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from oft.application.budget_ledger import BudgetLedger
    from oft.application.llm_orchestrator import LLMOrchestrator
    from oft.application.llm_validation import schema_text
    from oft.clock import FixedClock
    from oft.ports.llm import LLMRequest, LLMResult, LLMRole, LLMUsage

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "opportunity_valid.json"
    text = fixture.read_text(encoding="utf-8")

    class _Provider:
        provider = "openai"

        def complete(self, request: LLMRequest) -> LLMResult:
            return LLMResult(
                provider="openai", model="gpt-fixture", raw_text=text, usage=LLMUsage(1, 1)
            )

        def capabilities(self):
            from oft.ports.llm import ProviderCapabilities

            return ProviderCapabilities(provider="openai", strict_schema=True)

        def estimate_cost(self, request, model):
            raise NotImplementedError

    store = _store(tmp_path)
    ledger = BudgetLedger(store=store, price_table=_table())
    ledger.set_thresholds(daily_usd="100.00", updated_at=NOW)
    orchestrator = LLMOrchestrator(
        store=store,
        clock=FixedClock(datetime(2026, 11, 9, tzinfo=UTC)),
        provider=_Provider(),
        budget=ledger,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    request = LLMRequest(
        role=LLMRole.ANALYSIS,
        model="gpt-fixture",
        system_prompt="system",
        user_prompt="user",
        schema_json=schema_text(),
        input_hash="ih",
        prompt_id="analysis",
        prompt_version=1,
        prompt_hash="ph",
    )

    orchestrator.complete_with_validation(
        request=request, decision_id="DEC-REPEAT", role=LLMRole.ANALYSIS
    )
    orchestrator.complete_with_validation(
        request=request, decision_id="DEC-REPEAT", role=LLMRole.ANALYSIS
    )

    calls = store.list_llm_calls(decision_id="DEC-REPEAT")
    assert len(calls) == 2
    assert calls[0].call_id != calls[1].call_id
    budgets = store.list_budget_records(decision_id="DEC-REPEAT")
    assert len(budgets) == 2
    assert {budget.budget_state for budget in budgets} == {"OK"}


def test_partial_pricing_overrides_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = BudgetService(store=store)
    _call(store, "CALL-OVR")

    for kwargs in (
        {"unpriced": None, "cost_usd": "1.23", "price_version": None},
        {"unpriced": False, "cost_usd": None, "price_version": "v"},
        {"unpriced": True, "cost_usd": "1.23", "price_version": "v"},
        {"unpriced": False, "cost_usd": "0", "price_version": "v"},
    ):
        with pytest.raises(BudgetConfigError):
            service.record_call(
                call_id="CALL-OVR",
                decision_id=None,
                role="ANALYSIS",
                result=_result(),
                occurred_at=NOW,
                **kwargs,
            )


def _llm_call(call_id: str, *, decision_id: str | None = None) -> LLMCallRecord:
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
        created_at=NOW,
        decision_id=decision_id,
    )


def _threshold_events(store: SqliteStore) -> list[dict]:
    return [
        json.loads(event.payload_json)["payload"]
        for event in store.list_audit_events()
        if event.event_type == "BUDGET_THRESHOLD"
    ]


def test_a_malformed_ledger_timestamp_does_not_break_record_call(tmp_path: Path) -> None:
    """A legacy row with an unparsable occurred_at must not break recording (F2)."""
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    _call(store, "CALL-BAD")
    store.save_budget_record(
        BudgetRecord(
            record_id="BUD-BAD",
            call_id="CALL-BAD",
            decision_id=None,
            occurred_at="not-a-timestamp",
            provider="openai",
            model="gpt-fixture",
            cost_usd="0",
            price_version="none/v1",
            unpriced=True,
            budget_state="OK",
            period_day="2020-01-01",
            payload_json="{}",
            period_week="",
            period_month="",
            role="ANALYSIS",
        )
    )

    entry = _record(store, service, "CALL-GOOD")

    assert entry.record.budget_state == "EXCEEDED"
    assert store.list_budget_records(decision_id="DEC-1")


def test_a_day_crossing_is_not_masked_by_the_month_state(tmp_path: Path) -> None:
    """Per-period ranks come from pre-call totals, not the global max (F2/F3)."""
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="40.00", monthly_usd="15.00", updated_at=NOW)

    for index in range(4):
        _record(store, service, f"CALL-DAY-{index}")

    events = _threshold_events(store)
    day_events = [event for event in events if event["period"] == "day"]
    month_events = [event for event in events if event["period"] == "month"]
    # list_audit_events returns newest first; the set of crossings is the point.
    assert sorted(event["state"] for event in day_events) == ["EXCEEDED", "WARN"]
    assert sorted(event["state"] for event in month_events) == ["EXCEEDED", "WARN"]


def test_the_call_budget_and_threshold_audit_commit_together(tmp_path: Path) -> None:
    """One billed invocation writes one call row, one budget row, one audit (F4)."""
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)

    stored_call, entry = service.record_billed_call(
        call=_llm_call("CALL-ATOMIC", decision_id="DEC-ATOMIC"),
        result=_result(),
        occurred_at=NOW,
    )

    calls = store.list_llm_calls(decision_id="DEC-ATOMIC")
    budgets = store.list_budget_records(decision_id="DEC-ATOMIC")
    events = _threshold_events(store)
    assert [call.call_id for call in calls] == ["CALL-ATOMIC"]
    assert len(budgets) == 1
    assert budgets[0].record_id == entry.record.record_id
    assert stored_call.call_id == "CALL-ATOMIC"
    assert len(events) == 1
    assert events[0]["state"] == "WARN"


def test_a_repeated_billed_call_is_fully_recorded_under_a_unique_id(
    tmp_path: Path,
) -> None:
    """The adapter allocates a suffixed id when the base id already exists (F4)."""
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)

    first_call, _ = service.record_billed_call(
        call=_llm_call("CALL-REPEAT"), result=_result(), occurred_at=NOW
    )
    second_call, _ = service.record_billed_call(
        call=_llm_call("CALL-REPEAT"), result=_result(), occurred_at=NOW
    )

    assert first_call.call_id != second_call.call_id
    assert first_call.call_id == "CALL-REPEAT"
    assert second_call.call_id.startswith("CALL-REPEAT-")
    assert len(store.list_llm_calls()) == 2
    assert len(store.list_budget_records()) == 2
    assert len(_threshold_events(store)) == 2


def test_a_budget_record_id_collision_never_returns_a_phantom_entry(
    tmp_path: Path,
) -> None:
    """A budget insert that affects no row rolls back the whole call (db-3 LOW)."""
    from oft.domain.budget_periods import budget_record_id
    from oft.ports.store import BudgetRecordConflictError

    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    call = _llm_call("CALL-CONFLICT", decision_id="DEC-CONFLICT")
    # A different call already owns the deterministic record id this call would
    # derive, so its budget insert resolves to ON CONFLICT DO NOTHING.
    _call(store, "CALL-OTHER")
    store.save_budget_record(
        BudgetRecord(
            record_id=budget_record_id("CALL-CONFLICT"),
            call_id="CALL-OTHER",
            decision_id=None,
            occurred_at=NOW,
            provider="openai",
            model="gpt-fixture",
            cost_usd="0",
            price_version="none/v1",
            unpriced=True,
            budget_state="OK",
            period_day="2026-11-09",
            payload_json="{}",
        )
    )

    with pytest.raises(BudgetRecordConflictError):
        service.record_billed_call(call=call, result=_result(), occurred_at=NOW)

    # The call row was rolled back with the failed budget insert; the planted
    # row is the only budget row and no phantom entry escaped.
    assert store.list_llm_calls(decision_id="DEC-CONFLICT") == ()
    assert [row.call_id for row in store.list_budget_records()] == ["CALL-OTHER"]


def test_concurrent_atomic_writes_do_not_lose_a_crossing_audit(tmp_path: Path) -> None:
    """Only one writer can observe each rank increase under BEGIN IMMEDIATE (F4)."""
    store = _store(tmp_path)
    service = BudgetService(store=store, price_table=_table())
    service.set_thresholds(daily_usd="15.00", updated_at=NOW)
    errors: list[BaseException] = []

    def record(index: int) -> None:
        try:
            service.record_billed_call(
                call=_llm_call(f"CALL-RACE-{index}"),
                result=_result(),
                occurred_at=NOW,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    usage = service.usage("day", at=NOW)
    assert usage["call_count"] == 8
    assert usage["spend_usd"] == "100.00000000"
    # Exactly the OK->WARN and WARN->EXCEEDED crossings; the rest are already
    # EXCEEDED and must not re-audit.
    states = [event["state"] for event in _threshold_events(store) if event["period"] == "day"]
    assert sorted(states) == ["EXCEEDED", "WARN"]
