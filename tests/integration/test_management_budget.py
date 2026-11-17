"""Integration: every automated management call is budgeted and never hard-stops."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_management_flow import _setup

from oft.adapters.llm_stub import LLMStub
from oft.application.budget_service import BudgetService
from oft.application.jobs import DONE, JobLedger, ManagementJobRunner
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import ManagementService
from oft.clock import FixedClock
from oft.domain.time import utc_timestamp
from oft.ports.llm import CostEstimate, LLMRole
from oft.ports.store import BudgetSettingsRecord

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
NOW = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)


def _runner(setup: dict[str, Any], service: Any) -> ManagementJobRunner:
    container = setup["container"]
    return ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
    )


def _budget_audits(container: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in container.store.list_audit_events_ascending():
        if event.event_type != "BUDGET_THRESHOLD":
            continue
        body = json.loads(event.payload_json)
        payload = body.get("payload") if isinstance(body, dict) else None
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _management_budget_records(container: Any) -> list[Any]:
    return [
        record for record in container.store.list_budget_records() if record.role == "MANAGEMENT"
    ]


def test_every_automated_management_call_writes_a_budget_row_and_usage(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    budget = BudgetService(store=container.store, settings=setup["settings"])
    baseline = budget.usage("day", at=NOW_TEXT)["call_count"]

    outcome = _runner(setup, setup["service"]).run_window(NOW)

    assert outcome.status == DONE
    calls = container.store.list_llm_calls(role="MANAGEMENT")
    assert len(calls) == 1
    records = _management_budget_records(container)
    assert len(records) == 1
    assert records[0].decision_id is not None
    usage = budget.usage("day", at=NOW_TEXT)
    assert usage["call_count"] == baseline + 1


class _PricedProvider(LLMStub):
    """A provider seam that reports a priced call so a threshold can be crossed."""

    def estimate_cost(self, request: object, model: str) -> CostEstimate:  # type: ignore[override]
        return CostEstimate(amount_usd="2.00", price_version="test/v1", unpriced=False)


def test_a_threshold_crossing_alerts_locally_and_never_hard_stops(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    settings = setup["settings"]
    container.store.save_budget_settings(
        BudgetSettingsRecord(
            daily_usd="2.50",
            weekly_usd="50.00",
            monthly_usd="150.00",
            warn_pct=80.0,
            updated_at=NOW_TEXT,
        )
    )
    provider = _PricedProvider(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "management_hold.json"},
    )
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
        runtime=container.llm_runtime,
    )

    outcome = _runner(setup, service).run_window(NOW)

    # The call still returns and its decision is written: alerts never block.
    assert outcome.status == DONE
    assert outcome.results[0].action == "HOLD"
    assert len(container.store.list_decisions(role="MANAGEMENT")) == 1
    records = _management_budget_records(container)
    assert len(records) == 1
    assert records[0].budget_state == "WARN"
    assert records[0].cost_usd != "0"
    usage = BudgetService(store=container.store, settings=settings).usage("day", at=NOW_TEXT)
    assert usage["budget_state"] == "WARN"
    audits = _budget_audits(container)
    assert audits and audits[-1]["state"] == "WARN"
    assert audits[-1]["period"] == "day"

    # The management job never touches the Telegram path: no Telegram SDK is
    # imported, and no Telegram alert/delivery audit is written by this job.
    assert "telegram" not in sys.modules
    assert not any(
        "TELEGRAM" in event.event_type.upper()
        for event in container.store.list_audit_events_ascending()
    )
