"""Expired or invalidated opportunities never produce a trigger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.application.opportunity_service import OpportunityService
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot
from oft.ports.store import OpportunityRecord

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
PAST = utc_timestamp(NOW - timedelta(minutes=5))
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _insert_opportunity(
    store: SqliteStore,
    *,
    opportunity_id: str,
    state: str = "OPEN",
    expires_at: str = "2099-01-01T00:00:00.000Z",
) -> str:
    payload = {
        "opportunity_id": opportunity_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": state,
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "zone": {"low": "69000.00", "high": "71000.00"},
        "entry_preference": "MARKET",
        "trigger_conditions": [
            {
                "condition_id": "C-1",
                "draft_id": "c1",
                "type": "PRICE_TOUCH_ZONE",
                "params": {"price_low": "69900.00", "price_high": "70100.00"},
                "window": WINDOW,
                "required_data": ["CONTRACT_SPECS", "MARK_PRICE"],
                "evaluable": True,
            }
        ],
        "invalidation_conditions": [],
        "expires_at": expires_at,
        "thesis": "test",
        "confidence": 0.7,
        "risk_request": {},
        "source": {"report_id": "RPT-1"},
        "provenance": {},
        "data_quality": {},
        "state_history": [
            {
                "from_state": None,
                "to_state": state,
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
            state=state,
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )
    return opportunity_id


class FixedProvider:
    def snapshot_for(self, opportunity: object) -> TriggerSnapshot:
        return TriggerSnapshot(
            symbol="BTCUSDT",
            as_of=NOW_TEXT,
            last_price=Decimal("70000.00"),
            previous_price=Decimal("69900.00"),
            tick_size=Decimal("0.10"),
            data_quality="COMPLETE",
            snapshot_id="SNAP-TEST",
        )


def _service(store: SqliteStore) -> TriggerService:
    return TriggerService(store=store, clock=FixedClock(NOW), provider=FixedProvider())


def test_the_batch_expires_due_opportunities_before_evaluating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, opportunity_id="OPP-EXPIRED", expires_at=PAST)

    outcome = _service(store).evaluate_all(NOW_TEXT)

    assert outcome.open == 0
    assert outcome.triggered == 0
    assert store.list_trigger_events() == ()
    expired = OpportunityService(store=store, clock=FixedClock(NOW)).show("OPP-EXPIRED")
    assert expired.state.value == "EXPIRED"


def test_manual_evaluation_of_a_due_opportunity_expires_it_without_a_trigger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, opportunity_id="OPP-DUE", expires_at=PAST)

    run = _service(store).evaluate("OPP-DUE")

    assert run.trigger is None
    assert run.opportunity.state.value == "EXPIRED"
    assert store.list_trigger_events() == ()


def test_invalidated_opportunities_are_not_in_the_batch_and_never_trigger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, opportunity_id="OPP-INVALID", state="INVALIDATED")

    outcome = _service(store).evaluate_all(NOW_TEXT)

    assert outcome.open == 0
    assert outcome.triggered == 0
    assert store.list_trigger_events() == ()


def test_a_mixed_batch_only_triggers_the_open_opportunity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(store, opportunity_id="OPP-EXPIRED", expires_at=PAST)
    _insert_opportunity(store, opportunity_id="OPP-INVALID", state="INVALIDATED")
    _insert_opportunity(store, opportunity_id="OPP-OPEN")

    outcome = _service(store).evaluate_all(NOW_TEXT)

    assert outcome.open == 1
    assert outcome.triggered == 1
    records = store.list_trigger_events()
    assert len(records) == 1
    assert records[0].opportunity_id == "OPP-OPEN"
