"""Integration: the scan-aligned management cadence with one decision per window.

One OPEN position is built by the real entry flow (stub provider) and a second
is opened through the paper broker on the same recorded source chain, so the
prompt and the decision binding are exercised for both. Every claim is the real
SQLite job ledger on a fixed UTC clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_management_flow import _position_payload, _setup
from test_position_monitor_job import _runner as _position_monitor_runner
from test_position_monitor_job import _StubScheduler

import oft.application.jobs as jobs_module
from oft.adapters.llm_stub import LLMStub
from oft.application.jobs import (
    DONE,
    FAILED,
    PARTIAL,
    POSITION_MANAGE_JOB_TYPE,
    JobLedger,
    ManagementJobRunner,
    ManagementPositionStatus,
    latest_job_summary,
)
from oft.application.kill_switch import KillSwitch
from oft.application.management_add import AddGateInputs, evaluate_add
from oft.application.management_service import (
    DAILY_EQUITY_STATE_KEY,
    FUNDING_SETTLEMENT_CONDITION,
    RISK_BREACH_CONDITION,
    ManagementRejected,
    ManagementRiskContext,
    ManagementService,
)
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.position_service import PositionCloseError
from oft.application.scheduler import ScheduleState, align_to_boundary
from oft.bootstrap import build_container
from oft.cli.commands import daemon as daemon_module
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.positions import position_from_contract
from oft.domain.risk import BindingCap, RiskRequest, Verdict, verdict_inputs_hash
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
NOW = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)
WINDOW_START = utc_timestamp(align_to_boundary(NOW, "4h"))


def _runner(
    setup: dict[str, Any],
    *,
    now: datetime = NOW,
    period: str = "4h",
    funding_settlement_enabled: bool = True,
    risk_breach_enabled: bool = True,
) -> ManagementJobRunner:
    return ManagementJobRunner(
        service=setup["service"],
        ledger=JobLedger(store=setup["container"].store, clock=FixedClock(now)),
        clock=FixedClock(now),
        period=period,
        funding_settlement_enabled=funding_settlement_enabled,
        risk_breach_enabled=risk_breach_enabled,
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


class _RecordingStub(LLMStub):
    """A deterministic stub that keeps every rendered request for assertions."""

    def __init__(self, *, fixture: str) -> None:
        super().__init__(
            fixtures_dir=FIXTURES,
            overrides={LLMRole.MANAGEMENT: fixture},
        )
        self.requests: list[Any] = []

    def complete(self, request: Any) -> Any:
        self.requests.append(request)
        return super().complete(request)


def _seed_day_start(container: Any, day: datetime, equity: str) -> None:
    container.store.set_system_state(
        DAILY_EQUITY_STATE_KEY,
        json.dumps({"day": day.date().isoformat(), "equity": equity}),
        updated_at=NOW_TEXT,
    )


class _LogRecorder:
    """Capture the jobs module's structured warnings deterministically."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


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
    # A typed refusal is REFUSED (not a hard failure and not a DONE row).
    assert result.status == ManagementPositionStatus.REFUSED
    assert result.refusal_code == "KILL_SWITCH_ACTIVE"
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

    # A hard failure dominates the aggregate, so the window reports FAILED.
    assert outcome.status == FAILED
    results = {item.position_id: item for item in outcome.results}
    assert results[broken].status == ManagementPositionStatus.FAILED
    assert results[broken].reason_code == "JOB_FAILED"
    assert results[healthy].status == ManagementPositionStatus.DONE
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

    # Every position is skipped for the same reason, so the window is SKIPPED.
    assert outcome.status == "SKIPPED"
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
        day_start_equity=Decimal("11100.00"),
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


# -- C1: the automated ADD obeys the account-level daily-loss cap -------------


def test_an_unattended_add_is_blocked_after_the_daily_loss_cap_is_breached(
    tmp_path: Path,
) -> None:
    """C1: a measured breach blocks the unattended ADD and no order is created."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    size_before = Decimal(_position_payload(container, position_id)["size"])
    # The entry happened at the setup clock (00:00); make the recorded mark
    # fresh for this window's 00:05 clock before measuring.
    set_position_mark(
        container,
        position_id,
        _position_payload(container, position_id)["mark_price"],
        as_of=NOW_TEXT,
    )
    measurement = setup["service"].risk_context(now=NOW_TEXT)
    assert measurement.measured is True and measurement.equity > 0
    day_start = measurement.equity * Decimal("1.05")
    _seed_day_start(container, NOW, str(day_start))
    expected_loss = day_start - measurement.equity
    orders_before = _open_order_purposes(container)

    outcome = _runner(setup).run_window(NOW)

    result = outcome.results[0]
    assert result.status == ManagementPositionStatus.REFUSED
    assert result.refusal_code == "RISK_BLOCKED"
    assert outcome.status == PARTIAL
    assert _open_order_purposes(container) == orders_before
    assert Decimal(_position_payload(container, position_id)["size"]) == size_before
    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT binding_cap, daily_loss_usd FROM risk_verdicts "
            "WHERE risk_verdict_id LIKE 'RISK-MGD-%'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "DAILY_LOSS"
    assert rows[0][1] == str(expected_loss)


def test_the_add_gate_persists_the_daily_loss_in_the_bound_digest(tmp_path: Path) -> None:
    """The execution digest must re-read the same measured daily loss."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    payload = _position_payload(container, setup["position"]["position_id"])
    position = position_from_contract(payload)
    contract = container.store.get_symbol(position.symbol)
    assert contract is not None
    from oft.application.entry_service import effective_caps

    caps, _source = effective_caps(setup["settings"], container.store)
    entry_decision = container.store.list_decisions(role="ENTRY_RECHECK")[0]
    gate = AddGateInputs(
        store=container.store,
        decision_id=entry_decision.decision_id,
        position=position,
        mark=Decimal(payload["mark_price"]),
        mark_as_of=NOW_TEXT,
        now=NOW_TEXT,
        caps=caps,
        contract=contract,
        equity=Decimal("10000"),
        open_positions=1,
        stale_inputs=(),
        requested_quantity=Decimal("0.01"),
        requested_pct=0.5,
        daily_loss_usd=Decimal("500"),
    )

    evaluated = evaluate_add(gate)

    assert evaluated.verdict.verdict is Verdict.BLOCK
    assert evaluated.verdict.binding_cap is BindingCap.DAILY_LOSS
    stored = container.store.get_risk_verdict(evaluated.risk_verdict_id)
    assert stored is not None
    assert stored.daily_loss_usd == "500"
    expected_hash = verdict_inputs_hash(
        RiskRequest(
            symbol=position.symbol,
            side=position.side.value,
            entry_price=gate.mark,
            stop_price=position.stop_price,
            quantity=Decimal("0.01"),
            risk_pct=0.5,
            leverage=position.leverage,
            notional_usd=None,
        ),
        equity=Decimal("10000"),
        open_positions=1,
        daily_loss_usd=Decimal("500"),
    )
    assert json.loads(stored.payload_json)["inputs_hash"] == expected_hash


# -- H3: due conditions are threaded into the decision ------------------------


def _condition_service(
    setup: dict[str, Any], fixture: str
) -> tuple[ManagementService, _RecordingStub]:
    provider = _RecordingStub(fixture=fixture)
    service = ManagementService(
        store=setup["container"].store,
        clock=setup["container"].clock,
        provider=provider,
        settings=setup["settings"],
        kill_switch=KillSwitch(
            settings=setup["settings"],
            store=setup["container"].store,
            clock=setup["container"].clock,
        ),
    )
    return service, provider


def _breaching_context() -> ManagementRiskContext:
    return ManagementRiskContext(
        day=NOW.date(),
        realized_net=Decimal("0"),
        unrealized_net=Decimal("0"),
        equity=Decimal("10000"),
        day_start_equity=Decimal("11000"),
    )


def test_enabled_due_conditions_reach_prompt_provenance_and_ledger(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service, provider = _condition_service(setup, "management_hold.json")
    monkeypatch.setattr(
        service._positions,
        "funding_settlement_in_window",
        lambda *args, **kwargs: "2026-10-08T02:00:00.000Z",
    )
    monkeypatch.setattr(service, "risk_context", lambda **kwargs: _breaching_context())
    runner = ManagementJobRunner(
        service,
        JobLedger(store=container.store, clock=FixedClock(NOW)),
        FixedClock(NOW),
        period="4h",
    )

    outcome = runner.run_window(NOW)

    assert outcome.status == DONE
    assert provider.requests
    prompt = provider.requests[-1].user_prompt
    assert FUNDING_SETTLEMENT_CONDITION in prompt
    assert RISK_BREACH_CONDITION in prompt
    decisions = container.store.list_decisions(role="MANAGEMENT")
    envelope = json.loads(decisions[-1].payload_json)
    assert envelope["provenance"]["due_conditions"] == [
        "CADENCE",
        "FUNDING_SETTLEMENT",
        "RISK_BREACH",
    ]
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.message is not None
    assert "conditions=CADENCE,FUNDING_SETTLEMENT,RISK_BREACH" in run.message
    outcome_events = [
        json.loads(event.payload_json)["payload"]
        for event in container.store.list_audit_events_ascending()
        if event.event_type == "MANAGEMENT_OUTCOME"
    ]
    assert outcome_events[-1]["due_conditions"] == [
        "CADENCE",
        "FUNDING_SETTLEMENT",
        "RISK_BREACH",
    ]
    # F: a same-key replay reconstructs the conditions from the stored
    # decision provenance instead of returning an empty tuple.
    replay = service.manage(
        setup["position"]["position_id"],
        idempotency_key=f"auto-4h-{WINDOW_START}",
    )
    assert replay.idempotent_replay is True
    assert replay.due_conditions == ("CADENCE", "FUNDING_SETTLEMENT", "RISK_BREACH")
    assert replay.to_contract()["due_conditions"] == [
        "CADENCE",
        "FUNDING_SETTLEMENT",
        "RISK_BREACH",
    ]


def test_disabled_due_conditions_are_absent_and_their_reads_are_skipped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service, provider = _condition_service(setup, "management_hold.json")
    funding_calls: list[tuple[Any, ...]] = []
    risk_calls: list[dict[str, Any]] = []

    def _funding(*args: Any, **kwargs: Any) -> str:
        funding_calls.append(args)
        return "2026-10-08T02:00:00.000Z"

    monkeypatch.setattr(service._positions, "funding_settlement_in_window", _funding)
    monkeypatch.setattr(
        service,
        "risk_context",
        lambda **kwargs: risk_calls.append(kwargs) or _breaching_context(),
    )
    runner = ManagementJobRunner(
        service,
        JobLedger(store=container.store, clock=FixedClock(NOW)),
        FixedClock(NOW),
        period="4h",
        funding_settlement_enabled=False,
        risk_breach_enabled=False,
    )

    outcome = runner.run_window(NOW)

    assert outcome.status == DONE
    assert funding_calls == []
    assert risk_calls == []
    prompt = provider.requests[-1].user_prompt
    assert FUNDING_SETTLEMENT_CONDITION not in prompt
    assert RISK_BREACH_CONDITION not in prompt
    envelope = json.loads(container.store.list_decisions(role="MANAGEMENT")[-1].payload_json)
    assert envelope["provenance"]["due_conditions"] == ["CADENCE"]
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.message is not None and "conditions=CADENCE" in run.message


# -- M4: deterministic reclaim of an expired per-position row -----------------


def test_an_expired_running_window_is_reclaimed_and_replays_without_a_duplicate(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, management_fixture="management_partial_close.json")
    container = setup["container"]
    _runner(setup, now=NOW).run_window(NOW)
    calls = _management_calls(container)
    decisions = _management_decisions(container)
    orders = _open_order_purposes(container)
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == DONE
    database = setup["settings"].data_dir / "oft.db"
    expired = utc_timestamp(NOW - timedelta(hours=1))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE job_runs SET status = 'RUNNING', finished_at = NULL, "
            "lease_expires_at = ? WHERE run_id = ?",
            (expired, run.run_id),
        )
        connection.commit()

    later = NOW + timedelta(hours=1)  # still inside the same 4h window
    outcome = _runner(setup, now=later).run_window(later)

    assert outcome.status == DONE
    assert outcome.results[0].status == ManagementPositionStatus.DONE
    # The reclaimed row kept its run id and finished DONE.
    reclaimed = container.store.get_job_run(run.run_id)
    assert reclaimed is not None and reclaimed.status == DONE
    # The stored decision replayed: no second decision, order, or LLM call.
    assert _management_calls(container) == calls
    assert _management_decisions(container) == decisions
    assert _open_order_purposes(container) == orders


# -- M9: a stale/foreign window is refused without claiming -------------------


def test_a_foreign_window_is_refused_without_claiming(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    foreign = align_to_boundary(NOW + timedelta(hours=4), "4h")

    outcome = _runner(setup).run_window(NOW, window_start=foreign)

    assert outcome.status == "SKIPPED"
    assert outcome.reason == "window_not_current"
    assert container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE) == ()
    assert _management_calls(container) == 0
    assert _management_decisions(container) == 0


# -- H4: the restart catch-up runs the current window exactly once -------------


def test_restart_catch_up_runs_the_current_window_once(tmp_path: Path) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    state = ScheduleState(period="4h")

    first = daemon_module._catch_up_management_on_start(
        container, setup["service"], ledger, setup["settings"], state
    )

    assert first is not None and first.status == DONE
    assert _management_calls(container) == 1
    assert len(container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)) == 1

    second = daemon_module._catch_up_management_on_start(
        container, setup["service"], ledger, setup["settings"], state
    )

    assert second is not None and second.status == "SKIPPED"
    assert _management_calls(container) == 1
    assert len(container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)) == 1


# -- M15/M17: transient failures vs. routine typed skips ----------------------


def test_a_transient_provider_failure_finishes_failed_for_retry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise ManagementRejected("LLM_TIMEOUT", "the provider timed out")

    monkeypatch.setattr(service, "manage", _timeout)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == FAILED
    assert outcome.results[0].status == ManagementPositionStatus.FAILED
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == FAILED
    assert run.error_code == "LLM_TIMEOUT"


def test_a_position_close_error_finishes_a_typed_skip(tmp_path: Path, monkeypatch: Any) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    service = setup["service"]

    def _stale(*args: Any, **kwargs: Any) -> Any:
        raise PositionCloseError("DATA_STALE", "the recorded mark is stale")

    monkeypatch.setattr(service, "manage", _stale)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == "SKIPPED"
    result = outcome.results[0]
    assert result.status == ManagementPositionStatus.SKIPPED
    assert result.reason_code == "DATA_STALE"
    assert result.refusal_code is None
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == "SKIPPED"
    assert run.error_code == "DATA_STALE"


def test_an_invalid_position_state_finishes_a_typed_skip(tmp_path: Path, monkeypatch: Any) -> None:
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    setup_service = setup["service"]

    def _invalid(*args: Any, **kwargs: Any) -> Any:
        raise PositionCloseError("POSITION_STATE_INVALID", "the position is CLOSED, not OPEN")

    monkeypatch.setattr(setup_service, "manage", _invalid)
    runner = _runner(setup)
    result = runner.run_window(NOW).results[0]

    assert result.status == ManagementPositionStatus.SKIPPED
    assert result.reason_code == "POSITION_STATE_INVALID"


# -- M18: DONE outranks SKIPPED in the window summary ------------------------


def test_latest_job_summary_prefers_done_over_skipped(tmp_path: Path) -> None:
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
    ledger.finish(two.run.run_id, "SKIPPED", error_code="NOT_DUE")

    summary = latest_job_summary(container.store, POSITION_MANAGE_JOB_TYPE)

    assert summary is not None
    assert summary.status == DONE


def test_an_unmeasured_daily_loss_refuses_the_unattended_add(tmp_path: Path) -> None:
    """C1: an unmeasured loss must fail closed, never pass ``None`` to the gate."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    set_position_mark(
        container,
        position_id,
        _position_payload(container, position_id)["mark_price"],
        as_of=NOW_TEXT,
    )
    size_before = Decimal(_position_payload(container, position_id)["size"])
    orders_before = _open_order_purposes(container)
    # A corrupt journal row makes the realized measure unusable.
    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_logs "
            "(trade_id, position_id, account_id, environment, symbol, side, opened_at, "
            "closed_at, net_pnl_usd, idempotency_key, payload_json, order_id, fill_id, "
            "risk_verdict_id, decision_id) "
            "VALUES ('TRADE-BAD', 'POS-OLD', 'ACC-PAPER', 'PAPER', 'BTCUSDT', 'LONG', ?, "
            "'not-a-timestamp', '-400.00', 'IDEM-BAD', '{}', 'ORD', 'FILL', 'RISK', 'DEC')",
            (NOW_TEXT,),
        )
        connection.commit()

    outcome = _runner(setup).run_window(NOW)

    result = outcome.results[0]
    assert result.status == ManagementPositionStatus.REFUSED
    assert result.refusal_code == "DATA_UNAVAILABLE"
    assert outcome.status == PARTIAL
    assert _open_order_purposes(container) == orders_before
    assert Decimal(_position_payload(container, position_id)["size"]) == size_before


# -- Final round: silent-failure N1, restart reclaim, direct gate guard -------


def test_a_conflict_position_close_error_fails_the_window(tmp_path: Path, monkeypatch: Any) -> None:
    """N1: a CONFLICT is a hard failure, never a silent SKIPPED row."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    recorder = _LogRecorder()
    monkeypatch.setattr(jobs_module, "_LOGGER", recorder)

    def _conflict(*args: Any, **kwargs: Any) -> Any:
        raise PositionCloseError("CONFLICT", "the position changed concurrently")

    monkeypatch.setattr(setup["service"], "manage", _conflict)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == FAILED
    result = outcome.results[0]
    assert result.status == ManagementPositionStatus.FAILED
    assert result.reason_code == "CONFLICT"
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == FAILED
    assert run.error_code == "CONFLICT"
    assert any(
        event == "management_position_closed_error" and kwargs.get("error_code") == "CONFLICT"
        for event, kwargs in recorder.events
    )


def test_a_gate_position_close_error_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    """N1: a gate/broker refusal is REFUSED with its typed code, not FAILED."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    recorder = _LogRecorder()
    monkeypatch.setattr(jobs_module, "_LOGGER", recorder)

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise PositionCloseError("RISK_BLOCKED", "the risk verdict blocks this intent")

    monkeypatch.setattr(setup["service"], "manage", _blocked)

    outcome = _runner(setup).run_window(NOW)

    assert outcome.status == PARTIAL
    result = outcome.results[0]
    assert result.status == ManagementPositionStatus.REFUSED
    assert result.refusal_code == "RISK_BLOCKED"
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == DONE
    assert run.error_code == "RISK_BLOCKED"
    assert any(
        event == "management_position_closed_error" and kwargs.get("error_code") == "RISK_BLOCKED"
        for event, kwargs in recorder.events
    )


def test_the_direct_add_gate_refuses_an_unmeasured_daily_loss(tmp_path: Path) -> None:
    """C3: a direct caller cannot pass ``None`` and skip the cap."""
    setup = _setup(tmp_path, management_fixture="management_add.json")
    container = setup["container"]
    payload = _position_payload(container, setup["position"]["position_id"])
    position = position_from_contract(payload)
    contract = container.store.get_symbol(position.symbol)
    assert contract is not None
    from oft.application.entry_service import effective_caps
    from oft.application.management_add import AddGateError, persist_add_execution_verdict

    caps, _source = effective_caps(setup["settings"], container.store)
    entry_decision = container.store.list_decisions(role="ENTRY_RECHECK")[0]
    gate = AddGateInputs(
        store=container.store,
        decision_id=entry_decision.decision_id,
        position=position,
        mark=Decimal(payload["mark_price"]),
        mark_as_of=NOW_TEXT,
        now=NOW_TEXT,
        caps=caps,
        contract=contract,
        equity=Decimal("10000"),
        open_positions=1,
        stale_inputs=(),
        requested_quantity=Decimal("0.01"),
        requested_pct=0.5,
        daily_loss_usd=None,
    )

    with pytest.raises(AddGateError) as evaluate_error:
        evaluate_add(gate)
    assert evaluate_error.value.code == "DATA_UNAVAILABLE"

    with pytest.raises(AddGateError) as persist_error:
        persist_add_execution_verdict(
            gate, allowed_quantity=Decimal("0.01"), binding_cap=BindingCap.NONE
        )
    assert persist_error.value.code == "DATA_UNAVAILABLE"

    database = setup["settings"].data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM risk_verdicts WHERE risk_verdict_id LIKE 'RISK-MGD-%'"
        ).fetchone()
    assert rows == (0,)


def test_startup_reclaims_an_unexpired_crashed_management_row(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """B: startup expires the lease and the catch-up reclaims/replays the row."""
    setup = _setup(tmp_path, management_fixture="management_hold.json")
    container = setup["container"]
    _runner(setup, now=NOW).run_window(NOW)
    run = container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0]
    assert run.status == DONE
    calls = _management_calls(container)
    decisions = _management_decisions(container)
    orders = _open_order_purposes(container)
    database = setup["settings"].data_dir / "oft.db"
    future = utc_timestamp(NOW + timedelta(minutes=30))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE job_runs SET status = 'RUNNING', finished_at = NULL, "
            "error_code = NULL, message = NULL, lease_expires_at = ? WHERE run_id = ?",
            (future, run.run_id),
        )
        connection.commit()
    monkeypatch.setattr(daemon_module, "ManagementDaemonScheduler", _StubScheduler)
    paper_settings = container.settings.model_copy(update={"env": "paper"})

    daemon_module._start_management_scheduler(
        paper_settings, ScheduleState(period="4h"), container=container
    )

    reclaimed = container.store.get_job_run(run.run_id)
    assert reclaimed is not None
    assert reclaimed.status == DONE
    # The stored decision replayed: the run id, decision, order, and LLM count
    # are all unchanged.
    assert _management_calls(container) == calls
    assert _management_decisions(container) == decisions
    assert _open_order_purposes(container) == orders
    assert container.store.list_job_runs(POSITION_MANAGE_JOB_TYPE)[0].run_id == run.run_id
