"""Integration: the 15 s position_monitor tick with no LLM and a durable claim.

The positions are built through the paper broker directly (no scan, no LLM), so
``llm_calls`` is genuinely empty before and after the tick. The ledger is the
real SQLite job ledger and every comparison is on a fixed UTC clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from oft.application.jobs import (
    DONE,
    PARTIAL,
    POSITION_MANAGE_JOB_TYPE,
    POSITION_MONITOR_JOB_TYPE,
    PROTECTIVE_STOP,
    JobLedger,
    PositionMonitorRunner,
    align_interval,
)
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.position_service import PositionService
from oft.application.scheduler import ScheduleState, align_to_boundary
from oft.bootstrap import build_container
from oft.cli.commands import daemon as daemon_module
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.time import utc_timestamp
from position_support import set_position_mark

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _container(tmp_path: Path) -> Any:
    settings = _settings(tmp_path)
    return build_container(settings, initialize=True, clock=FixedClock(NOW))


def _seed_entry_parents(container: Any, suffix: str) -> None:
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES (?, 'BTCUSDT', 'TRIGGERED', 't', 't', '{}', NULL)",
            (f"OPP-{suffix}",),
        )
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES (?, ?, 'BTCUSDT', 'PENDING_RECHECK', ?, 't', '{}')",
            (f"TRG-{suffix}", f"OPP-{suffix}", f"TRG-{suffix}-KEY"),
        )
        connection.execute(
            "INSERT INTO decisions "
            "(decision_id, role, decision_type, validation_status, raw_output_hash, "
            "persisted_at, payload_json) VALUES (?, 'ENTRY_RECHECK', 'ENTRY_RECHECK', "
            "'VALID', 'H', 't', '{}')",
            (f"DEC-{suffix}",),
        )
        connection.execute(
            "INSERT INTO risk_verdicts "
            "(risk_verdict_id, trigger_id, decision_id, verdict, binding_cap, computed_at, "
            "expires_at, payload_json) VALUES (?, ?, ?, 'ALLOW', 'NONE', 't', 't', '{}')",
            (f"RISK-{suffix}", f"TRG-{suffix}", f"DEC-{suffix}"),
        )
        connection.commit()


def _open_long(container: Any, suffix: str, *, stop: str = "69000") -> str:
    """Open one LONG position through the broker (no LLM, no scan)."""
    _seed_entry_parents(container, suffix)
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal(stop),
        take_profits=(),
        leverage=2.0,
        decision_id=f"DEC-{suffix}",
        risk_verdict_id=f"RISK-{suffix}",
        approval_id=f"APR-{suffix}",
        idempotency_key=f"entry-{suffix}",
        position_side=PositionSide.LONG,
        risk_amount_usd=Decimal("10"),
        risk_pct=0.1,
        trigger_id=f"TRG-{suffix}",
    )
    entered = broker.submit(
        intent,
        mark_price=Decimal("70000"),
        mark_as_of=NOW_TEXT,
        now=NOW_TEXT,
    )
    position_id = entered.position.position_id
    set_position_mark(container, position_id, "70000", as_of=NOW_TEXT)
    return position_id


def _runner(container: Any, *, now: datetime = NOW) -> PositionMonitorRunner:
    service = PositionService(
        store=container.store, clock=FixedClock(now), settings=container.settings
    )
    ledger = JobLedger(store=container.store, clock=FixedClock(now))
    return PositionMonitorRunner(service, ledger, FixedClock(now))


def _result_for(outcome: Any, position_id: str) -> Any:
    return next(item for item in outcome.results if item.position_id == position_id)


def test_one_tick_closes_only_the_stop_hit_position_with_protective_stop(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    hit = _open_long(container, "HIT")
    keep = _open_long(container, "KEEP")
    set_position_mark(container, hit, "69000", as_of=NOW_TEXT)
    set_position_mark(container, keep, "72000", as_of=NOW_TEXT)
    assert container.store.list_llm_calls() == ()

    outcome = _runner(container).run_tick(NOW)

    assert outcome.claimed is True
    assert outcome.run is not None and outcome.run.status == DONE
    hit_result = _result_for(outcome, hit)
    keep_result = _result_for(outcome, keep)
    assert hit_result.status == "CLOSED"
    assert hit_result.reason_code == PROTECTIVE_STOP
    assert keep_result.status == "NO_ACTION"
    assert keep_result.reason_code == "STOP_NOT_TOUCHED"
    assert outcome.positions_total == 2
    assert hit in outcome.closed_position_ids

    open_ids = [record.position_id for record in container.store.list_positions(state="OPEN")]
    assert open_ids == [keep]

    trades = container.store.list_trade_logs(limit=None)
    assert len(trades) == 1
    assert trades[0].position_id == hit
    trade = json.loads(trades[0].payload_json)
    # The journal's outcome authority stays in the domain's closed vocabulary;
    # the code-only reason is carried by the job outcome.
    assert "STOPPED" in trade["outcome_tags"]
    assert container.store.list_llm_calls() == ()
    assert container.store.list_decisions(role="MANAGEMENT") == ()


def test_a_stale_mark_yields_no_action_and_a_visible_data_stale_reason(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    position_id = _open_long(container, "STALE")
    stale_at = utc_timestamp(NOW - timedelta(seconds=120))
    set_position_mark(container, position_id, "69000", as_of=stale_at)

    outcome = _runner(container).run_tick(NOW)

    result = _result_for(outcome, position_id)
    assert result.status == "NO_ACTION"
    assert result.reason_code == "DATA_STALE"
    assert outcome.run is not None and outcome.run.status == DONE
    assert len(container.store.list_positions(state="OPEN")) == 1
    assert container.store.list_trade_logs(limit=None) == ()


def test_a_second_tick_in_the_same_window_loses_the_claim_and_writes_nothing(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    position_id = _open_long(container, "TICK")
    set_position_mark(container, position_id, "69000", as_of=NOW_TEXT)
    runner = _runner(container)

    first = runner.run_tick(NOW)
    trades_after_first = container.store.list_trade_logs(limit=None)
    second = runner.run_tick(NOW)

    assert first.claimed is True
    assert second.claimed is False
    assert second.run is not None and first.run is not None
    assert second.run.run_id == first.run.run_id
    assert second.reason == "duplicate"
    assert len(container.store.list_job_runs(POSITION_MONITOR_JOB_TYPE)) == 1
    assert container.store.list_trade_logs(limit=None) == trades_after_first
    assert len(trades_after_first) == 1


def test_one_raising_position_does_not_abort_the_tick_and_marks_it_partial(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    broken = _open_long(container, "BROKEN")
    fine = _open_long(container, "FINE")
    set_position_mark(container, broken, "72000", as_of=NOW_TEXT)
    set_position_mark(container, fine, "72000", as_of=NOW_TEXT)
    real = PositionService(
        store=container.store, clock=FixedClock(NOW), settings=container.settings
    )

    class _FailingService:
        def monitor(self, position_id: str) -> Any:
            if position_id == broken:
                raise RuntimeError("boom")
            return real.monitor(position_id)

    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    runner = PositionMonitorRunner(_FailingService(), ledger, FixedClock(NOW))  # type: ignore[arg-type]

    outcome = runner.run_tick(NOW)

    assert outcome.claimed is True
    assert outcome.run is not None and outcome.run.status == PARTIAL
    assert outcome.errors == (broken,)
    assert _result_for(outcome, fine).status == "NO_ACTION"


def test_the_monitor_tick_uses_no_llm_even_for_the_protective_close(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    position_id = _open_long(container, "NOLLM")
    set_position_mark(container, position_id, "69000", as_of=NOW_TEXT)

    # The runner has no provider field at all; the close is code-only.
    outcome = _runner(container).run_tick(NOW)

    assert _result_for(outcome, position_id).status == "CLOSED"
    assert container.store.list_llm_calls() == ()
    assert container.store.list_budget_records() == ()


class _StubScheduler:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True

    def shutdown(self, **kwargs: Any) -> None:
        pass


def test_the_daemon_reaps_stale_monitor_runs_and_starts_the_interval_scheduler(
    tmp_path: Path, monkeypatch: Any
) -> None:
    container = _container(tmp_path)
    past = NOW - timedelta(minutes=30)
    claim = JobLedger(store=container.store, clock=FixedClock(past)).claim(
        POSITION_MONITOR_JOB_TYPE,
        utc_timestamp(align_interval(past, 15)),
        source=POSITION_MONITOR_JOB_TYPE,
    )
    assert claim.claimed is True
    monkeypatch.setattr(daemon_module, "PositionMonitorDaemonScheduler", _StubScheduler)

    scheduler = daemon_module._start_position_monitor_scheduler(
        container.settings, container=container
    )

    assert scheduler.started is True
    assert scheduler.kwargs["interval_seconds"] == (
        container.settings.position_monitor_interval_seconds
    )
    runs = container.store.list_job_runs(POSITION_MONITOR_JOB_TYPE)
    assert [(run.status, run.error_code) for run in runs] == [("FAILED", "LEASE_EXPIRED")]


def test_the_daemon_management_scheduler_uses_the_scan_period_and_no_positions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    container = _container(tmp_path)
    monkeypatch.setattr(daemon_module, "ManagementDaemonScheduler", _StubScheduler)
    state = ScheduleState(period="4h")

    scheduler = daemon_module._start_management_scheduler(
        container.settings, state, container=container
    )

    assert scheduler.started is True
    assert scheduler.kwargs["period_provider"]() == "4h"
    # The live scan schedule state is wired in so a pending period change
    # re-arms at its transition boundary instead of being skipped.
    assert scheduler.kwargs["state_provider"]() == state
    window = align_to_boundary(NOW, "4h")
    outcome = scheduler.kwargs["job"](window)
    assert outcome.status == DONE
    runs = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)
    assert len(runs) == 1
    assert runs[0].window_start == utc_timestamp(window)
    assert runs[0].status == DONE


def test_daemon_status_includes_the_latest_monitor_and_management_jobs(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    monitor = ledger.claim(
        POSITION_MONITOR_JOB_TYPE,
        utc_timestamp(align_interval(NOW, 15)),
        source=POSITION_MONITOR_JOB_TYPE,
    )
    assert monitor.claimed and monitor.run is not None
    ledger.finish(monitor.run.run_id, DONE, message="open=0")
    window = align_to_boundary(NOW, "4h")
    manage = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        utc_timestamp(window),
        source="position_manage",
        period="4h",
    )
    assert manage.claimed and manage.run is not None
    ledger.finish(manage.run.run_id, "SKIPPED", error_code="NO_LLM_PROVIDER")

    payload = daemon_module._status_payload(container.settings, ScheduleState(period="4h"))

    assert payload["jobs"]["position_monitor"]["status"] == DONE
    assert payload["jobs"]["position_monitor"]["finished_at"] == NOW_TEXT
    assert payload["jobs"]["position_manage"]["status"] == "SKIPPED"
    assert payload["jobs"]["position_manage"]["error_code"] == "NO_LLM_PROVIDER"
    assert payload["jobs"]["position_manage"]["window_start"] == utc_timestamp(window)
