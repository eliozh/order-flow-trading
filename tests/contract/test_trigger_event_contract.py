"""Persisted trigger events validate the contract on both the manual and job paths."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import _helpers
from _registry import validate

from oft.application.jobs import JobLedger, TriggerJobRunner
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock


def _scanned_container(tmp_path: Path) -> Any:
    resolved = _helpers.settings(tmp_path)
    container = build_container(resolved, initialize=True, clock=FixedClock(_helpers.NOW))
    backfill = container.backfill.run(
        symbol="BTCUSDT", data_type="aggTrades", start=_helpers.START, end=_helpers.END
    )
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    return container


def _assert_trigger_contract(payload: dict[str, Any]) -> None:
    validate("trigger-event.schema.json", payload)
    assert payload["trigger_source"] == "CODE"
    assert payload["status"] == "PENDING_RECHECK"
    assert payload["created_order_id"] is None
    snapshot = payload["snapshot"]
    assert snapshot["market_snapshot_id"]
    assert snapshot["snapshot_hash"]
    assert snapshot["as_of"]
    assert snapshot["data_quality"] in {
        "COMPLETE",
        "PARTIAL",
        "STALE",
        "MISSING",
        "ESTIMATED",
    }
    assert payload["condition_results"]
    for result in payload["condition_results"]:
        assert set(result) == {
            "condition_id",
            "type",
            "met",
            "observed_value",
            "threshold",
            "evaluated_at",
            "data_quality",
            "evaluable",
            "note",
        }
        assert result["evaluated_at"]
        assert result["data_quality"] in {"COMPLETE", "PARTIAL", "STALE", "MISSING"}


def test_the_manual_path_persists_a_contract_valid_trigger(tmp_path: Path) -> None:
    container = _scanned_container(tmp_path)
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None

    payload = json.loads(run.trigger.payload_json)

    _assert_trigger_contract(payload)
    assert payload["opportunity_id"] == opportunity.opportunity_id


def test_the_job_path_persists_a_contract_valid_trigger(tmp_path: Path) -> None:
    container = _scanned_container(tmp_path)
    ledger = JobLedger(store=container.store, clock=container.clock)
    service = TriggerService(store=container.store, clock=container.clock)
    runner = TriggerJobRunner(
        service=service,
        ledger=ledger,
        clock=container.clock,
        interval_seconds=15,
    )

    outcome = runner.run_tick(_helpers.NOW + timedelta(seconds=1))

    assert outcome.counts.triggered == 1
    records = container.store.list_trigger_events()
    assert len(records) == 1
    payload = json.loads(records[0].payload_json)
    _assert_trigger_contract(payload)
    assert records[0].snapshot_hash == payload["snapshot"]["snapshot_hash"]
    assert records[0].as_of == payload["snapshot"]["as_of"]
    assert records[0].data_quality == payload["snapshot"]["data_quality"]
    assert records[0].condition_count == len(payload["condition_results"])
