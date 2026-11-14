"""Integration: the scan-aligned management cadence with one decision per window.

One OPEN position is built by the real entry flow (stub provider) and a second
is opened through the paper broker on the same recorded source chain, so the
prompt and the decision binding are exercised for both. Every claim is the real
SQLite job ledger on a fixed UTC clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from test_management_flow import _position_payload, _setup
from test_position_monitor_job import _runner as _position_monitor_runner

from oft.adapters.llm_stub import LLMStub
from oft.application.jobs import (
    DONE,
    FAILED,
    PARTIAL,
    POSITION_MANAGE_JOB_TYPE,
    JobLedger,
    ManagementJobRunner,
    latest_job_summary,
)
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    RISK_BREACH_CONDITION,
    ManagementRiskContext,
    ManagementService,
)
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.scheduler import align_to_boundary
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.time import utc_timestamp
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
NOW = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
WINDOW_START = utc_timestamp(align_to_boundary(NOW, "4h"))


def _runner(
    setup: dict[str, Any], *, now: datetime = NOW, period: str = "4h"
) -> ManagementJobRunner:
    return ManagementJobRunner(
        service=setup["service"],
        ledger=JobLedger(store=setup["container"].store, clock=FixedClock(now)),
        clock=FixedClock(now),
        period=period,
    )


def _management_calls(container: Any) -> int:
    return len(container.store.list_llm_calls(role="MANAGEMENT"))


def _record_due_conditions(monkeypatch: Any, service: ManagementService) -> list[tuple[str, ...]]:
    """Collect the conditions each per-position due evaluation reports."""
    seen: list[tuple[str, ...]] = []
    real_due = service.due_conditions

    def _recording(position_id: str, **kwargs: Any) -> Any:
        due = real_due(position_id, **kwargs)
        seen.append(due.conditions)
        return due

    monkeypatch.setattr(service, "due_conditions", _recording)
    return seen


def _management_decisions(container: Any) -> int:
    return len(container.store.list_decisions(role="MANAGEMENT"))


def _open_order_purposes(container: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for order in container.store.list_orders(limit=1000):
        counts[order.purpose] = counts.get(order.purpose, 0) + 1
    return counts


def _second_open_position(container: Any, *, suffix: str = "SECOND") -> str:
    """Open a second LONG on the recorded source chain (no extra LLM call)."""
    first = container.store.list_positions(state="OPEN")[0]
    first_payload = json.loads(first.payload_json)
    trigger = container.store.get_trigger_event(str(first_payload["source_trigger_id"]))
    assert trigger is not None
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES (?, ?, 'BTCUSDT', 'ACCEPTED', ?, ?, '{}')",
            (f"TRG-{suffix}", trigger.opportunity_id, f"TRG-{suffix}-KEY", NOW_TEXT),
        )
        connection.execute(
            "INSERT INTO decisions "
            "(decision_id, role, decision_type, validation_status, raw_output_hash, "
            "persisted_at, payload_json) VALUES (?, 'ENTRY_RECHECK', 'ENTRY_RECHECK', "
            "'VALID', 'H', ?, '{}')",
            (f"DEC-{suffix}", NOW_TEXT),
        )
        connection.execute(
            "INSERT INTO risk_verdicts "
            "(risk_verdict_id, trigger_id, decision_id, verdict, binding_cap, computed_at, "
            "expires_at, payload_json) VALUES (?, ?, ?, 'ALLOW', 'NONE', ?, ?, '{}')",
            (f"RISK-{suffix}", f"TRG-{suffix}", f"DEC-{suffix}", NOW_TEXT, NOW_TEXT),
        )
        connection.commit()
    broker = PaperBroker(settings=container.settings, store=container.store, clock=container.clock)
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("69000"),
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
        mark_price=Decimal("70002"),
        mark_as_of=NOW_TEXT,
        now=NOW_TEXT,
    )
    position_id = entered.position.position_id
    set_position_mark(container, position_id, "70002", as_of=NOW_TEXT)
    return position_id


def test_one_window_calls_the_llm_once_per_open_position(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    second = _second_open_position(container)
    assert _management_calls(container) == 0

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == DONE
    assert len(outcome.results) == 2
    assert all(result.status == DONE for result in outcome.results)
    assert _management_calls(container) == 2
    assert _management_decisions(container) == 2
    # HOLD creates no order; only the two entries exist.
    assert _open_order_purposes(container) == {"ENTRY": 2}
    runs = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)
    assert len(runs) == 2
    assert {run.window_start for run in runs} == {
        f"{WINDOW_START}#{setup['position']['position_id']}",
        f"{WINDOW_START}#{second}",
    }
    # ``symbol`` is a market-symbol column; per-position rows use the compound
    # window key instead of overloading it with a position id.
    assert all(run.symbol is None for run in runs)


def test_a_second_window_at_the_same_clock_is_a_ledger_no_op(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    runner = _runner(setup)

    runner.run_window(NOW)
    calls = _management_calls(container)
    decisions = _management_decisions(container)

    second = runner.run_window(NOW)

    assert calls == 1
    assert _management_calls(container) == calls
    assert _management_decisions(container) == decisions
    assert len(container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)) == 1
    assert second.results and all(result.claimed is False for result in second.results)


def test_a_restart_runner_at_the_same_window_replays_without_a_duplicate(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    _runner(setup).run_window(NOW)
    calls = _management_calls(container)
    decisions = _management_decisions(container)
    orders = _open_order_purposes(container)

    restarted = _runner(setup).run_window(NOW)

    assert _management_calls(container) == calls
    assert _management_decisions(container) == decisions
    assert _open_order_purposes(container) == orders
    assert len(container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)) == 1
    assert all(result.claimed is False for result in restarted.results)


def test_an_overlapping_claim_is_skipped_and_writes_nothing(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    key = f"{WINDOW_START}#{position_id}"
    claim = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        key,
        source="position_manage",
        symbol=position_id,
        period="4h",
    )
    assert claim.claimed is True

    outcome = _runner(setup).run_window(NOW)

    assert _management_calls(container) == 0
    assert outcome.results[0].claimed is False
    assert outcome.results[0].reason_code in {"duplicate", "already_running"}


def test_kill_switch_blocks_the_automated_add_but_not_the_protective_exit(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    setup["kill"].activate(actor_id="tester")
    orders_before = _open_order_purposes(container)
    size_before = _position_payload(container, position_id)["size"]

    outcome = _runner(setup).run_window(NOW)

    result = outcome.results[0]
    assert result.reason_code == "KILL_SWITCH_ACTIVE"
    assert result.rejected is True
    assert outcome.status == PARTIAL
    assert _open_order_purposes(container) == orders_before
    assert _position_payload(container, position_id)["size"] == size_before

    # The same setup's stop-hit position still closes through the code-only tick.
    set_position_mark(container, position_id, position["stop_price"], as_of=NOW_TEXT)
    tick = _position_monitor_runner(container, now=NOW).run_tick(NOW)
    assert tick.claimed is True
    assert _position_payload(container, position_id)["state"] == "CLOSED"
    assert len(container.store.list_trade_logs(limit=None)) == 1


def test_no_provider_finishes_skipped_without_calling_manage(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    settings = setup["settings"]
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=None,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
    )

    outcome = runner.run_window(NOW)

    assert outcome.results[0].status == "SKIPPED"
    assert outcome.results[0].reason_code == "NO_LLM_PROVIDER"
    assert _management_calls(container) == 0
    assert _management_decisions(container) == 0


def test_an_empty_open_set_makes_no_llm_call_and_finishes_done(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    provider = LLMStub(fixtures_dir=FIXTURES)
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
    )

    outcome = runner.run_window(NOW)

    assert outcome.status == DONE
    assert outcome.results == ()
    assert container.store.list_llm_calls() == ()
    runs = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)
    assert len(runs) == 1
    assert runs[0].status == DONE
    assert runs[0].window_start == WINDOW_START


def test_a_raising_due_evaluation_fails_one_position_and_runs_the_rest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    healthy = setup["position"]["position_id"]
    broken = _second_open_position(container)
    service = setup["service"]
    real_due = service.due_conditions

    def _flaky(position_id: str, **kwargs: Any) -> Any:
        if position_id == broken:
            raise RuntimeError("transient store read failed")
        return real_due(position_id, **kwargs)

    monkeypatch.setattr(service, "due_conditions", _flaky)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == PARTIAL
    results = {item.position_id: item for item in outcome.results}
    assert results[broken].status == FAILED
    assert results[broken].reason_code == "JOB_FAILED"
    assert results[healthy].status == DONE
    runs = {
        run.window_start: run for run in container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)
    }
    broken_row = runs[f"{WINDOW_START}#{broken}"]
    assert broken_row.status == FAILED
    assert broken_row.error_code == "JOB_FAILED"
    assert broken_row.finished_at is not None
    assert runs[f"{WINDOW_START}#{healthy}"].status == DONE
    # No claimed row is left RUNNING for the window.
    assert all(run.status != "RUNNING" for run in runs.values())
    assert _management_calls(container) == 1


def test_a_position_read_failure_fails_the_window_without_claiming(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]

    def _unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(container.store, "list_positions", _unavailable)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == FAILED
    assert outcome.reason == "position_read_failed"
    assert outcome.results == ()
    # No row was claimed before the read failed.
    assert container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE) == ()


def test_a_disabled_risk_breach_skips_the_account_level_measurement(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]
    real_measure = service.risk_context
    measure_calls = {"count": 0}

    def _counting(**kwargs: Any) -> Any:
        measure_calls["count"] += 1
        return real_measure(**kwargs)

    monkeypatch.setattr(service, "risk_context", _counting)
    seen = _record_due_conditions(monkeypatch, service)
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
        risk_breach_enabled=False,
    )

    outcome = runner.run_window(NOW)

    assert outcome.status == DONE
    assert _management_calls(container) == 1
    # The disabled condition's account-level inputs are not measured.
    assert measure_calls["count"] == 0
    assert all(RISK_BREACH_CONDITION not in conditions for conditions in seen)


def test_an_unconfigured_provider_does_not_measure_the_account(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    settings = setup["settings"]
    service = ManagementService(
        store=container.store,
        clock=container.clock,
        provider=None,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )
    real_logs = container.store.list_trade_logs
    calls = {"count": 0}

    def _counting(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return real_logs(*args, **kwargs)

    monkeypatch.setattr(container.store, "list_trade_logs", _counting)
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
    )

    outcome = runner.run_window(NOW)

    assert outcome.status == DONE
    assert [item.reason_code for item in outcome.results] == ["NO_LLM_PROVIDER"]
    # No management provider and no measurement path reads the trade log.
    assert calls["count"] == 0


def test_a_raising_risk_measure_does_not_abort_the_window(tmp_path: Path, monkeypatch: Any) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]

    def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("account read failed")

    monkeypatch.setattr(service, "risk_context", _boom)
    seen = _record_due_conditions(monkeypatch, service)

    outcome = _runner(setup).run_window(NOW)

    # The optional measurement failed; the cadence claim and decision still run.
    assert outcome.status == DONE
    assert [item.status for item in outcome.results] == [DONE]
    assert _management_calls(container) == 1
    assert all(RISK_BREACH_CONDITION not in conditions for conditions in seen)


def test_the_enabled_path_measures_once_and_reports_risk_breach(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]
    _second_open_position(container)
    # The measurement arithmetic is pinned by the unit tests; here a measured
    # breaching context isolates the runner's once-per-window wiring.
    breaching = ManagementRiskContext(
        day=NOW.date(),
        realized_net=Decimal("-400.00"),
        unrealized_net=Decimal("0"),
        equity=Decimal("10700.00"),
    )
    calls = {"count": 0}

    def _counting(**kwargs: Any) -> Any:
        calls["count"] += 1
        return breaching

    monkeypatch.setattr(service, "risk_context", _counting)
    seen = _record_due_conditions(monkeypatch, service)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == DONE
    # Two positions, one account-level measurement for the whole window.
    assert calls["count"] == 1
    assert any(RISK_BREACH_CONDITION in conditions for conditions in seen)


def test_the_account_level_daily_loss_inputs_are_read_once_per_window(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]
    _second_open_position(container)
    real = service.risk_context
    calls = {"count": 0}

    def _counting(**kwargs: Any) -> Any:
        calls["count"] += 1
        return real(**kwargs)

    monkeypatch.setattr(service, "risk_context", _counting)
    runner = ManagementJobRunner(
        service=service,
        ledger=JobLedger(store=container.store, clock=FixedClock(NOW)),
        clock=FixedClock(NOW),
        period="4h",
    )

    outcome = runner.run_window(NOW)

    # Two positions, one account-level measurement: the per-position evaluation
    # reuses the frozen window context instead of measuring again.
    assert calls["count"] == 1
    assert [item.reason_code for item in outcome.results] == ["HOLD", "HOLD"]


def test_latest_job_summary_aggregates_a_mixed_window_as_partial(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    first = setup["position"]["position_id"]
    second = _second_open_position(container)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    one = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        f"{WINDOW_START}#{first}",
        source="position_manage",
        period="4h",
    )
    two = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        f"{WINDOW_START}#{second}",
        source="position_manage",
        period="4h",
    )
    assert one.claimed and one.run is not None and two.claimed and two.run is not None
    ledger.finish(one.run.run_id, DONE, message="action=HOLD status=NO_ACTION")
    ledger.finish(
        two.run.run_id,
        DONE,
        error_code="KILL_SWITCH_ACTIVE",
        message="the kill switch is active",
    )

    summary = latest_job_summary(container.store, POSITION_MANAGE_JOB_TYPE)

    assert summary is not None
    assert summary.status == PARTIAL
    assert summary.window_start == WINDOW_START
    assert "#" not in summary.window_start
    assert summary.error_code == "KILL_SWITCH_ACTIVE"


def test_latest_job_summary_prefers_failed_over_done(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    first = setup["position"]["position_id"]
    second = _second_open_position(container)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    one = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        f"{WINDOW_START}#{first}",
        source="position_manage",
        period="4h",
    )
    two = ledger.claim(
        POSITION_MANAGE_JOB_TYPE,
        f"{WINDOW_START}#{second}",
        source="position_manage",
        period="4h",
    )
    assert one.claimed and one.run is not None and two.claimed and two.run is not None
    ledger.finish(one.run.run_id, DONE, message="action=HOLD status=NO_ACTION")
    ledger.finish(two.run.run_id, FAILED, error_code="JOB_FAILED", message="read failed")

    summary = latest_job_summary(container.store, POSITION_MANAGE_JOB_TYPE)

    assert summary is not None
    assert summary.status == FAILED
    assert summary.window_start == WINDOW_START
    assert "#" not in summary.window_start
    assert summary.error_code == "JOB_FAILED"
