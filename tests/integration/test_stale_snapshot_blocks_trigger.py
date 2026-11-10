"""A stale or unavailable snapshot yields no trigger and a visible reason."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from oft.adapters.sqlite_store import SqliteStore
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.domain.trigger import TriggerSnapshot
from oft.ports.store import OpportunityRecord

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
STALE_TEXT = utc_timestamp(NOW - timedelta(seconds=120))
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    return store


def _insert_opportunity(
    store: SqliteStore,
    *,
    opportunity_id: str = "OPP-1",
    extra_conditions: list[dict[str, object]] | None = None,
) -> str:
    conditions: list[dict[str, object]] = [
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
    conditions.extend(extra_conditions or [])
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
        "source": {"report_id": "RPT-1"},
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


class FixedProvider:
    def __init__(self, snapshot: TriggerSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot_for(self, opportunity: object) -> TriggerSnapshot | None:
        return self._snapshot


def _snapshot(*, as_of: str = NOW_TEXT, data_quality: str = "COMPLETE") -> TriggerSnapshot:
    return TriggerSnapshot(
        symbol="BTCUSDT",
        as_of=as_of,
        last_price=Decimal("70000.00"),
        previous_price=Decimal("69900.00"),
        tick_size=Decimal("0.10"),
        data_quality=data_quality,
        snapshot_id="SNAP-TEST",
    )


def _service(store: SqliteStore, provider: FixedProvider) -> TriggerService:
    return TriggerService(store=store, clock=FixedClock(NOW), provider=provider)


def test_a_stale_mark_blocks_the_manual_and_batch_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _service(store, FixedProvider(_snapshot(as_of=STALE_TEXT)))

    manual = service.evaluate(opportunity_id)
    batch = service.evaluate_all(NOW_TEXT)

    assert manual.trigger is None
    assert manual.evaluation.stale is True
    assert any("stale snapshot" in reason for reason in manual.evaluation.reasons)
    assert batch.triggered == 0
    assert batch.stale == 1
    assert store.list_trigger_events() == ()


def test_a_snapshot_marked_stale_blocks_the_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _service(store, FixedProvider(_snapshot(data_quality="STALE")))

    run = service.evaluate(opportunity_id)

    assert run.trigger is None
    assert run.evaluation.stale is True
    assert "STALE" in run.evaluation.reasons[0]


def test_an_unavailable_snapshot_yields_no_trigger_and_a_visible_reason(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    opportunity_id = _insert_opportunity(store)
    service = _service(store, FixedProvider(None))

    run = service.evaluate(opportunity_id)
    batch = service.evaluate_all(NOW_TEXT)

    assert run.trigger is None
    assert any("DATA_UNAVAILABLE" in reason for reason in run.evaluation.reasons)
    assert batch.unavailable == 1
    assert batch.triggered == 0
    assert store.list_trigger_events() == ()


def test_evaluable_false_is_never_met_by_a_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_opportunity(
        store,
        extra_conditions=[
            {
                "condition_id": "C-2",
                "draft_id": "c2",
                "type": "CVD_ABOVE",
                "params": {"threshold": "50", "lookback_bars": 2},
                "window": WINDOW,
                "required_data": ["METRICS_5M"],
                "evaluable": False,
                "unavailable_reason": "CVD is unavailable",
            }
        ],
    )
    service = _service(store, FixedProvider(_snapshot()))

    batch = service.evaluate_all(NOW_TEXT)

    assert batch.triggered == 0
    assert store.list_trigger_events() == ()
