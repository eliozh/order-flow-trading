"""One trigger per (opportunity, snapshot): SQLite enforces the idempotency key."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.opportunity_service import OpportunityService
from oft.application.trigger_service import TriggerError, TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot, evaluate_trigger
from oft.ports.store import OpportunityRecord, TriggerEventRecord

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(_settings(tmp_path))
    store.initialize()
    return store


def _snapshot() -> TriggerSnapshot:
    return TriggerSnapshot(
        symbol="BTCUSDT",
        as_of=NOW_TEXT,
        last_price=Decimal("70000.00"),
        previous_price=Decimal("69900.00"),
        tick_size=Decimal("0.10"),
        data_quality="COMPLETE",
        snapshot_id="SNAP-TEST",
    )


class FixedProvider:
    def __init__(self, snapshot: TriggerSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot_for(self, opportunity: object) -> TriggerSnapshot:
        return self._snapshot


def _insert_opportunity(
    store: SqliteStore,
    opportunity_id: str = "OPP-1",
    *,
    trigger_conditions: list[dict[str, object]] | None = None,
    report_id: str = "RPT-1",
) -> str:
    conditions = trigger_conditions or [
        {
            "condition_id": "C-1",
            "draft_id": "c1",
            "type": "PRICE_TOUCH_ZONE",
            "params": {"price_low": "69900.00", "price_high": "70100.00"},
            "window": WINDOW,
            "required_data": ["CONTRACT_SPECS", "MARK_PRICE"],
            "evaluable": True,
        }
    ]
    payload = {
        "opportunity_id": opportunity_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "zone": {"low": "69000.00", "high": "71000.00"},
        "entry_preference": "MARKET",
        "trigger_conditions": conditions,
        "invalidation_conditions": [],
        "expires_at": "2099-01-01T00:00:00.000Z",
        "thesis": "test",
        "confidence": 0.7,
        "risk_request": {},
        "source": {"report_id": report_id},
        "provenance": {},
        "data_quality": {},
        "state_history": [
            {
                "from_state": None,
                "to_state": "OPEN",
                "at": NOW_TEXT,
                "reason_code": "CREATED",
                "note": "test",
            }
        ],
    }
    store.save_opportunity(
        OpportunityRecord(
            opportunity_id=opportunity_id,
            symbol="BTCUSDT",
            state="OPEN",
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )
    return opportunity_id


def _service(store: SqliteStore) -> TriggerService:
    return TriggerService(
        store=store,
        clock=FixedClock(NOW),
        provider=FixedProvider(_snapshot()),
    )


def test_repeated_evaluation_returns_the_existing_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _service(store)

    first = service.evaluate(opportunity_id)
    second = service.evaluate(opportunity_id)

    assert first.created is True
    assert first.trigger is not None
    assert second.created is False
    assert second.trigger is not None
    assert second.trigger.trigger_id == first.trigger.trigger_id
    assert len(store.list_trigger_events()) == 1


def test_an_existing_trigger_for_an_open_opportunity_is_not_duplicated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    opportunity = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    snapshot = _snapshot()
    evaluation = evaluate_trigger(
        opportunity.trigger_conditions,
        snapshot,
        symbol=opportunity.symbol,
        now=NOW_TEXT,
        snapshot_id=snapshot.snapshot_id,
    )
    key = f"TRG-{opportunity_id}-{evaluation.snapshot_hash[:16].upper()}"
    assert store.save_trigger_event(
        TriggerEventRecord(
            trigger_id="TRG-PREEXISTING",
            opportunity_id=opportunity_id,
            symbol=opportunity.symbol,
            status="PENDING_RECHECK",
            idempotency_key=key,
            triggered_at=NOW_TEXT,
            payload_json=json.dumps({"trigger_id": "TRG-PREEXISTING"}),
            snapshot_hash=evaluation.snapshot_hash,
            as_of=snapshot.as_of,
            data_quality="COMPLETE",
            condition_count=1,
        )
    )

    run = _service(store).evaluate(opportunity_id)

    assert run.created is False
    assert run.trigger is not None and run.trigger.trigger_id == "TRG-PREEXISTING"
    assert len(store.list_trigger_events()) == 1


def test_sqlite_enforces_the_unique_idempotency_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store)
    base = TriggerEventRecord(
        trigger_id="TRG-A",
        opportunity_id="OPP-1",
        symbol="BTCUSDT",
        status="PENDING_RECHECK",
        idempotency_key="TRG-OPP-1-DUPLICATE",
        triggered_at=NOW_TEXT,
        payload_json="{}",
        snapshot_hash="hash",
        as_of=NOW_TEXT,
        data_quality="COMPLETE",
        condition_count=1,
    )

    assert store.save_trigger_event(base) is True
    duplicate = TriggerEventRecord(
        trigger_id="TRG-B",
        opportunity_id="OPP-1",
        symbol="BTCUSDT",
        status="PENDING_RECHECK",
        idempotency_key="TRG-OPP-1-DUPLICATE",
        triggered_at=NOW_TEXT,
        payload_json="{}",
    )
    assert store.save_trigger_event(duplicate) is False
    assert len(store.list_trigger_events()) == 1
    assert store.get_trigger_by_idempotency("TRG-OPP-1-DUPLICATE") is not None


def test_manual_and_batch_produce_the_same_trigger_for_the_same_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-MANUAL")
    _insert_opportunity(store, "OPP-BATCH")
    service = _service(store)

    manual = service.evaluate("OPP-MANUAL")
    batch = service.evaluate_all(NOW_TEXT)

    assert manual.trigger is not None
    manual_payload = json.loads(manual.trigger.payload_json)
    stored = store.list_trigger_events()
    assert len(stored) == 2
    batch_payload = next(
        json.loads(record.payload_json) for record in stored if record.opportunity_id == "OPP-BATCH"
    )
    assert manual_payload["snapshot"] == batch_payload["snapshot"]
    assert manual_payload["condition_results"] == batch_payload["condition_results"]
    assert manual_payload["trigger_source"] == batch_payload["trigger_source"] == "CODE"
    assert batch.triggered == 1


def test_the_batch_is_idempotent_across_ticks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store)
    service = _service(store)

    first = service.evaluate_all(NOW_TEXT)
    second = service.evaluate_all(NOW_TEXT)

    assert first.triggered == 1
    assert second.open == 0
    assert second.triggered == 0
    assert len(store.list_trigger_events()) == 1


def _time_window_conditions() -> list[dict[str, object]]:
    """A condition evaluable from ``as_of`` alone, for the price-only override path."""
    return [
        {
            "condition_id": "C-1",
            "draft_id": "c1",
            "type": "TIME_WINDOW",
            "params": {},
            "window": WINDOW,
            "required_data": ["MARK_PRICE"],
            "evaluable": True,
        }
    ]


def test_manual_price_and_as_of_override_creates_a_trigger_with_the_supplied_as_of(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    supplied_as_of = "2026-11-06T11:59:30.000Z"  # 30 s old: inside the 60 s budget
    service = _service(store)

    run = service.evaluate(opportunity_id, price=Decimal("70000.00"), as_of=supplied_as_of)

    assert run.created is True
    assert run.trigger is not None
    payload = json.loads(run.trigger.payload_json)
    assert payload["snapshot"]["as_of"] == supplied_as_of
    assert payload["snapshot"]["market_snapshot_id"]
    assert payload["trigger_source"] == "CODE"


def test_manual_evaluation_falls_back_to_the_provider_for_the_missing_override(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    by_price = _insert_opportunity(
        store, "OPP-PRICE-ONLY", trigger_conditions=_time_window_conditions()
    )
    by_as_of = _insert_opportunity(
        store, "OPP-AS-OF-ONLY", trigger_conditions=_time_window_conditions()
    )
    supplied_as_of = "2026-11-06T11:59:30.000Z"
    service = _service(store)  # provider snapshot: SNAP-TEST @ NOW_TEXT

    price_run = service.evaluate(by_price, price=Decimal("70000.00"))
    as_of_run = service.evaluate(by_as_of, as_of=supplied_as_of)

    assert price_run.created is True
    assert price_run.trigger is not None
    assert json.loads(price_run.trigger.payload_json)["snapshot"]["as_of"] == NOW_TEXT
    assert as_of_run.created is True
    assert as_of_run.trigger is not None
    assert json.loads(as_of_run.trigger.payload_json)["snapshot"]["as_of"] == supplied_as_of


def test_manual_stale_as_of_yields_stale_and_creates_no_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    service = _service(store)

    run = service.evaluate(
        opportunity_id,
        price=Decimal("70000.00"),
        as_of="2026-11-06T11:00:00.000Z",  # one hour old: beyond the 60 s budget
    )

    assert run.trigger is None
    assert run.created is False
    assert run.evaluation.stale is True
    assert any("stale snapshot" in reason for reason in run.evaluation.reasons)
    assert store.list_trigger_events() == ()


class _RacingStore:
    """A store wrapper where another evaluator wins the OPEN->TRIGGERED race.

    The service has already read the opportunity as OPEN. Immediately before
    the real atomic insert+transition transaction runs, another writer moves
    the opportunity to INVALIDATED. The transaction's guard must fail, roll the
    trigger insert back, and report a conflict: no orphan PENDING_RECHECK row.
    """

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner
        self.transition_attempts = 0
        self.last_record: TriggerEventRecord | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def save_trigger_and_transition(self, record: Any, opportunity: Any, event: Any) -> str:
        self.transition_attempts += 1
        self.last_record = record
        OpportunityService(store=self._inner, clock=FixedClock(NOW)).invalidate(
            record.opportunity_id, reason="another evaluator won the race"
        )
        return self._inner.save_trigger_and_transition(record, opportunity, event)


def test_a_concurrent_transition_rolls_the_trigger_back_and_never_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import trigger_service as trigger_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, Any]]] = []

        def warning(self, event: str, **kwargs: Any) -> None:
            self.warnings.append((event, kwargs))

    logger = _RecordingLogger()
    monkeypatch.setattr(trigger_module, "_LOGGER", logger)

    inner = _store(tmp_path)
    opportunity_id = _insert_opportunity(inner)
    store = _RacingStore(inner)
    service = TriggerService(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        provider=FixedProvider(_snapshot()),
    )

    outcome = service.evaluate_all(NOW_TEXT)

    assert store.transition_attempts == 1
    assert outcome.evaluated == 1
    assert outcome.triggered == 0
    assert outcome.skipped == 1
    # Atomicity: the rolled-back trigger insert leaves no orphan row.
    assert inner.list_trigger_events() == ()
    stored = OpportunityService(store=inner, clock=FixedClock(NOW)).show(opportunity_id)
    assert stored.state.value == "INVALIDATED"
    assert [event for event, _ in logger.warnings] == ["trigger_transition_conflict"]
    assert store.last_record is not None
    assert logger.warnings[0][1]["trigger_id"] == store.last_record.trigger_id


def test_a_crash_replay_completes_the_transition_without_duplicating(
    tmp_path: Path,
) -> None:
    """A trigger committed before the crash reconciles exactly once."""
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    opportunity = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    snapshot = _snapshot()
    evaluation = evaluate_trigger(
        opportunity.trigger_conditions,
        snapshot,
        symbol=opportunity.symbol,
        now=NOW_TEXT,
        snapshot_id=snapshot.snapshot_id,
    )
    key = f"TRG-{opportunity_id}-{evaluation.snapshot_hash[:16].upper()}"
    trigger_id = "TRG-CRASHED"
    assert store.save_trigger_event(
        TriggerEventRecord(
            trigger_id=trigger_id,
            opportunity_id=opportunity_id,
            symbol=opportunity.symbol,
            status="PENDING_RECHECK",
            idempotency_key=key,
            triggered_at=NOW_TEXT,
            payload_json=json.dumps({"trigger_id": trigger_id}),
            snapshot_hash=evaluation.snapshot_hash,
            as_of=snapshot.as_of,
            data_quality="COMPLETE",
            condition_count=1,
        )
    )
    # The crash happened before the OPEN -> TRIGGERED transition committed.
    assert (
        OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id).state.value
        == "OPEN"
    )

    run = _service(store).evaluate(opportunity_id)

    assert run.created is False
    assert run.trigger is not None and run.trigger.trigger_id == trigger_id
    assert len(store.list_trigger_events()) == 1
    stored = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    assert stored.state.value == "TRIGGERED"
    triggered_entries = [
        entry
        for entry in OpportunityService(store=store, clock=FixedClock(NOW)).timeline(opportunity_id)
        if entry["to_state"] == "TRIGGERED"
    ]
    assert len(triggered_entries) == 1
    assert triggered_entries[0]["reason_code"] == "TRIGGER_MET"
    assert triggered_entries[0]["note"] == trigger_id


class _QualityProvider:
    def __init__(self, snapshot: TriggerSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot_for(self, opportunity: object) -> TriggerSnapshot:
        self.calls += 1
        return self._snapshot


def test_a_manual_override_cannot_launder_a_stale_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    provider = _QualityProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of="2026-11-06T11:59:30.000Z",
            last_price=Decimal("70000.00"),
            data_quality="STALE",
            quality_reasons=("metrics older than budget",),
            snapshot_id="SNAP-STALE",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    with pytest.raises(TriggerError) as caught:
        service.evaluate(opportunity_id, price=Decimal("70000.00"))

    assert caught.value.code == "VALIDATION_FAILED"
    assert "STALE" in caught.value.message
    assert "metrics older than budget" in caught.value.message
    assert store.list_trigger_events() == ()


def test_a_manual_override_preserves_the_source_report_quality(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    provider = _QualityProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            data_quality="PARTIAL",
            quality_reasons=("funding unavailable",),
            snapshot_id="SNAP-PARTIAL",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    run = service.evaluate(opportunity_id, as_of="2026-11-06T11:59:30.000Z")

    assert run.created is True
    assert run.trigger is not None
    assert run.evaluation.data_quality == "PARTIAL"
    payload = json.loads(run.trigger.payload_json)
    assert payload["snapshot"]["data_quality"] == "PARTIAL"


def test_both_overrides_resolve_the_source_report_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    provider = _QualityProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            tick_size=Decimal("0.10"),
            data_quality="PARTIAL",
            quality_reasons=("funding unavailable",),
            snapshot_id="SNAP-PARTIAL",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    run = service.evaluate(
        opportunity_id,
        price=Decimal("70000.00"),
        as_of="2026-11-06T11:59:30.000Z",
    )

    # The source report is always resolved, even when both overrides are
    # supplied: the provider is the STALE-quality gate.
    assert provider.calls == 1
    assert run.created is True
    assert run.trigger is not None
    assert run.evaluation.data_quality == "PARTIAL"


def test_both_overrides_on_a_stale_source_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    provider = _QualityProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            data_quality="STALE",
            quality_reasons=("metrics older than budget",),
            snapshot_id="SNAP-STALE",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    with pytest.raises(TriggerError) as caught:
        service.evaluate(
            opportunity_id,
            price=Decimal("70000.00"),
            as_of="2026-11-06T11:59:30.000Z",
        )

    assert caught.value.code == "VALIDATION_FAILED"
    assert "STALE" in caught.value.message
    assert provider.calls == 1
    assert store.list_trigger_events() == ()


def test_a_price_only_override_keeps_the_source_tick_size_and_evaluates(
    tmp_path: Path,
) -> None:
    """A ``--price`` override must merge into, not replace, the report snapshot."""
    store = _store(tmp_path)
    # PRICE_TOUCH_ZONE requires CONTRACT_SPECS (tick_size) and MARK_PRICE.
    opportunity_id = _insert_opportunity(store)
    provider = _QualityProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("69000.00"),  # outside the 69900-70100 zone
            tick_size=Decimal("0.10"),
            data_quality="COMPLETE",
            snapshot_id="SNAP-FULL",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    run = service.evaluate(opportunity_id, price=Decimal("70000.00"))

    assert run.created is True
    assert run.evaluation.results[0].evaluable is True
    assert run.evaluation.results[0].met is True


def test_snapshot_id_override_replaces_the_frozen_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)

    run = _service(store).evaluate(opportunity_id, snapshot_id="SNAP-OVERRIDE")

    assert run.created is True
    assert run.trigger is not None
    assert run.evaluation.snapshot_id == "SNAP-OVERRIDE"
    payload = json.loads(run.trigger.payload_json)
    assert payload["snapshot"]["market_snapshot_id"] == "SNAP-OVERRIDE"


def test_manual_override_without_a_report_cannot_resolve_as_of(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store, trigger_conditions=_time_window_conditions())
    service = TriggerService(
        store=store,
        clock=FixedClock(NOW),
        provider=FixedProvider(None),  # type: ignore[arg-type]
    )

    run = service.evaluate(opportunity_id, price=Decimal("70000.00"))

    assert run.trigger is None
    assert run.created is False
    assert any("DATA_UNAVAILABLE" in reason for reason in run.evaluation.reasons)


def test_manual_evaluation_of_a_non_open_opportunity_returns_or_conflicts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    triggered_id = _insert_opportunity(store, "OPP-TRIGGERED")
    service = _service(store)
    first = service.evaluate(triggered_id)

    again = service.evaluate(triggered_id)

    assert again.created is False
    assert again.trigger is not None
    assert first.trigger is not None
    assert again.trigger.trigger_id == first.trigger.trigger_id

    conflict_id = _insert_opportunity(store, "OPP-CONFLICT")
    OpportunityService(store=store, clock=FixedClock(NOW)).invalidate(
        conflict_id, reason="no longer valid"
    )
    with pytest.raises(TriggerError) as caught:
        service.evaluate(conflict_id)
    assert caught.value.code == "CONFLICT"


def test_an_empty_provider_snapshot_id_is_never_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    provider = FixedProvider(
        TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            previous_price=Decimal("69900.00"),
            tick_size=Decimal("0.10"),
            data_quality="COMPLETE",
            snapshot_id="",
        )
    )
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    run = service.evaluate(opportunity_id)

    assert run.trigger is not None
    snapshot = json.loads(run.trigger.payload_json)["snapshot"]
    assert snapshot["market_snapshot_id"].startswith("SNAP-")
    assert snapshot["market_snapshot_id"] == f"SNAP-{snapshot['snapshot_hash'][:16].upper()}"


class _PerReportProvider:
    """Returns a distinct snapshot per source report and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def snapshot_for(self, opportunity: Any) -> TriggerSnapshot:
        report_id = str(opportunity.source.get("report_id", ""))
        self.calls.append((opportunity.symbol, report_id))
        return TriggerSnapshot(
            symbol=opportunity.symbol,
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            previous_price=Decimal("69900.00"),
            tick_size=Decimal("0.10"),
            data_quality="COMPLETE",
            snapshot_id=f"SNAP-{report_id}",
        )


def test_two_opportunities_on_one_symbol_use_their_own_report_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-A", report_id="RPT-1")
    _insert_opportunity(store, "OPP-B", report_id="RPT-2")
    provider = _PerReportProvider()
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    outcome = service.evaluate_all(NOW_TEXT)

    assert sorted(provider.calls) == [("BTCUSDT", "RPT-1"), ("BTCUSDT", "RPT-2")]
    assert outcome.triggered == 2
    snapshot_ids = {
        json.loads(record.payload_json)["snapshot"]["market_snapshot_id"]
        for record in store.list_trigger_events()
    }
    assert snapshot_ids == {"SNAP-RPT-1", "SNAP-RPT-2"}


def test_two_opportunities_sharing_one_report_fetch_one_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-A", report_id="RPT-1")
    _insert_opportunity(store, "OPP-B", report_id="RPT-1")
    provider = _PerReportProvider()
    service = TriggerService(store=store, clock=FixedClock(NOW), provider=provider)

    outcome = service.evaluate_all(NOW_TEXT)

    # Cache key is ``(symbol, report_id)``: one provider call serves both.
    assert provider.calls == [("BTCUSDT", "RPT-1")]
    assert outcome.triggered == 2


def test_one_corrupt_open_payload_does_not_abort_the_tick(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-GOOD")
    store.save_opportunity(
        OpportunityRecord(
            opportunity_id="OPP-BAD",
            symbol="BTCUSDT",
            state="OPEN",
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json="{not-json",
        )
    )
    service = _service(store)

    outcome = service.evaluate_all(NOW_TEXT)

    assert outcome.open == 2
    assert outcome.evaluated == 1
    assert outcome.triggered == 1
    assert outcome.unavailable == 1
    assert len(store.list_trigger_events()) == 1


def test_the_batch_logs_and_reports_the_true_open_count_when_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import trigger_service as trigger_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, Any]]] = []

        def warning(self, event: str, **kwargs: Any) -> None:
            self.warnings.append((event, kwargs))

    logger = _RecordingLogger()
    monkeypatch.setattr(trigger_module, "_LOGGER", logger)
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-A")
    _insert_opportunity(store, "OPP-B")
    service = TriggerService(
        store=store,
        clock=FixedClock(NOW),
        provider=FixedProvider(_snapshot()),
        batch_cap=1,
    )

    outcome = service.evaluate_all(NOW_TEXT)

    assert outcome.open == 1
    assert outcome.open_total == 2
    assert outcome.capped is True
    capped = [kwargs for event, kwargs in logger.warnings if event == "trigger_batch_capped"]
    assert capped == [{"loaded": 1, "open_total": 2, "batch_cap": 1}]


def test_a_batch_exactly_at_the_cap_is_not_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import trigger_service as trigger_module

    class _RecordingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, Any]]] = []

        def warning(self, event: str, **kwargs: Any) -> None:
            self.warnings.append((event, kwargs))

    logger = _RecordingLogger()
    monkeypatch.setattr(trigger_module, "_LOGGER", logger)
    store = _store(tmp_path)
    _insert_opportunity(store, "OPP-A")
    service = TriggerService(
        store=store,
        clock=FixedClock(NOW),
        provider=FixedProvider(_snapshot()),
        batch_cap=1,
    )

    outcome = service.evaluate_all(NOW_TEXT)

    assert outcome.open == outcome.open_total == 1
    assert outcome.capped is False
    assert not [event for event, _ in logger.warnings if event == "trigger_batch_capped"]


def test_reload_logs_and_falls_back_when_the_row_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import trigger_service as trigger_module
    from oft.application.opportunity_service import OpportunityNotFound

    class _RecordingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, Any]]] = []

        def warning(self, event: str, **kwargs: Any) -> None:
            self.warnings.append((event, kwargs))

    class _VanishedOpportunities:
        def show(self, opportunity_id: str) -> Any:
            raise OpportunityNotFound(opportunity_id)

    logger = _RecordingLogger()
    monkeypatch.setattr(trigger_module, "_LOGGER", logger)
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    opportunity = OpportunityService(store=store, clock=FixedClock(NOW)).show(opportunity_id)
    service = _service(store)
    monkeypatch.setattr(service, "_opportunities", _VanishedOpportunities())

    returned = service._reload(opportunity)

    assert returned is opportunity
    assert logger.warnings[0][0] == "trigger_reload_failed"
    assert logger.warnings[0][1]["opportunity_id"] == opportunity_id
    assert "OPP-1" in str(logger.warnings[0][1]["error"])


def test_trigger_list_orders_equal_timestamps_by_trigger_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store)
    for trigger_id, key in (("TRG-B", "K-B"), ("TRG-A", "K-A"), ("TRG-C", "K-C")):
        assert store.save_trigger_event(
            TriggerEventRecord(
                trigger_id=trigger_id,
                opportunity_id="OPP-1",
                symbol="BTCUSDT",
                status="PENDING_RECHECK",
                idempotency_key=key,
                triggered_at=NOW_TEXT,
                payload_json="{}",
            )
        )

    assert [record.trigger_id for record in store.list_trigger_events()] == [
        "TRG-C",
        "TRG-B",
        "TRG-A",
    ]


def test_the_batch_transition_uses_the_triggered_at_not_the_clock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    later = "2026-11-06T12:00:09.000Z"  # differs from the FixedClock instant
    service = _service(store)

    outcome = service.evaluate_all(later)

    assert outcome.triggered == 1
    trigger = store.list_trigger_events()[0]
    assert trigger.triggered_at == later
    triggered_entries = [
        entry
        for entry in OpportunityService(store=store, clock=FixedClock(NOW)).timeline(opportunity_id)
        if entry["to_state"] == "TRIGGERED"
    ]
    assert triggered_entries[0]["at"] == later
    assert triggered_entries[0]["note"] == trigger.trigger_id
