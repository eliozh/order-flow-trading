"""Integration: the management cadence never replays a missed window (no catch-up)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_management_flow import _setup

from oft.application.jobs import POSITION_MANAGE_JOB_TYPE, JobLedger, ManagementJobRunner
from oft.application.scheduler import align_to_boundary
from oft.clock import FixedClock
from oft.domain.time import utc_timestamp

NOW = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)
LATER = NOW + timedelta(hours=12)


def _runner(setup: dict, now: datetime) -> ManagementJobRunner:  # type: ignore[type-arg]
    return ManagementJobRunner(
        service=setup["service"],
        ledger=JobLedger(store=setup["container"].store, clock=FixedClock(now)),
        clock=FixedClock(now),
        period="4h",
    )


def test_after_a_gap_only_the_current_window_is_claimed(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    position_id = setup["position"]["position_id"]

    _runner(setup, NOW).run_window(NOW)
    outcome = _runner(setup, LATER).run_window(LATER)

    current = utc_timestamp(align_to_boundary(LATER, "4h"))
    earlier = utc_timestamp(align_to_boundary(NOW, "4h"))
    runs = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)
    assert {run.window_start for run in runs} == {
        f"{earlier}#{position_id}",
        f"{current}#{position_id}",
    }
    # The three windows crossed by the gap were never claimed or replayed.
    assert all("#2026-10-08T04:00:00.000Z" not in run.window_start for run in runs)
    assert all("#2026-10-08T08:00:00.000Z" not in run.window_start for run in runs)
    assert outcome.results[0].status == "DONE"
    assert len(container.store.list_llm_calls(role="MANAGEMENT")) == 2
    assert len(container.store.list_decisions(role="MANAGEMENT")) == 2
