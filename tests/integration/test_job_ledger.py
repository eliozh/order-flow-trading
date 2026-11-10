"""Durable job ledger: unique claims, leases, reclaim, locks, restart state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.application.jobs import JobLedger, RestartState, ScheduledScanRunner
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import ScanSymbolRecord

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
WINDOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


def test_job_tables_are_created_idempotently(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    store.initialize()

    import sqlite3

    with sqlite3.connect(database_path(settings)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert {"job_runs", "job_locks", "scan_symbols"} <= tables
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19}


def test_duplicate_job_claim_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))

    first = ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-1")
    second = ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-2")

    assert first.claimed is True
    assert first.run is not None and first.run.status == "RUNNING"
    assert second.claimed is False
    assert second.run is not None and second.run.run_id == first.run.run_id
    assert len(store.list_job_runs("scan")) == 1


def test_unique_constraint_is_enforced_by_sqlite(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    assert ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-1").claimed

    import sqlite3

    with (
        pytest.raises(sqlite3.IntegrityError),
        sqlite3.connect(database_path(settings)) as connection,
    ):
        connection.execute(
            """
                INSERT INTO job_runs (
                    run_id, job_type, window_start, status, trigger_source,
                    started_at, finished_at, error_code, message, lease_owner,
                    lease_expires_at, heartbeat_at
                ) VALUES ('dup', 'scan', ?, 'RUNNING', 'TEST', 't', NULL, NULL, NULL,
                          'o', 't', 't')
                """,
            (utc_timestamp(WINDOW),),
        )


def test_stale_running_lease_is_reclaimed_and_restarts_safely(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FixedClock(NOW)
    ledger = JobLedger(store=store, clock=clock)
    first = ledger.claim("scan", utc_timestamp(WINDOW), owner="old-owner", lease_seconds=10)
    assert first.claimed and first.run is not None

    later = FixedClock(NOW + timedelta(seconds=60))
    resumed = JobLedger(store=store, clock=later)
    reclaimed = resumed.claim("scan", utc_timestamp(WINDOW), owner="new-owner", lease_seconds=10)

    assert reclaimed.claimed is True
    assert reclaimed.reclaimed is True
    assert reclaimed.run is not None
    # The reclaimed run keeps its identity, so checkpointed children resume.
    assert reclaimed.run.run_id == first.run.run_id
    assert reclaimed.run.lease_owner == "new-owner"
    assert reclaimed.run.status == "RUNNING"

    assert resumed.finish(reclaimed.run.run_id, "DONE")
    assert resumed.latest("scan") is not None
    assert resumed.latest("scan").status == "DONE"  # type: ignore[union-attr]


def test_a_fresh_running_lease_is_not_stolen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FixedClock(NOW)
    ledger = JobLedger(store=store, clock=clock)
    assert ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-1", lease_seconds=900).claimed

    stolen = JobLedger(store=store, clock=clock).claim(
        "scan", utc_timestamp(WINDOW), owner="owner-2", lease_seconds=900
    )
    assert stolen.claimed is False
    assert stolen.reclaimed is False


def test_missed_window_is_recorded_and_never_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))

    missed = ledger.record_missed(
        "scan",
        utc_timestamp(WINDOW - timedelta(hours=4)),
        reason="superseded by the latest missed window",
    )
    assert missed.status == "MISSED"
    assert "latest" in (missed.message or "")

    # A MISSED row is terminal: a later claim for the same window is skipped.
    duplicate = ledger.claim("scan", utc_timestamp(WINDOW - timedelta(hours=4)), owner="owner")
    assert duplicate.claimed is False


def test_named_lock_is_leased_and_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))

    assert ledger.acquire_scan_lock("owner-1", ttl_seconds=10) is True
    assert ledger.acquire_scan_lock("owner-2", ttl_seconds=10) is False
    lock = ledger.active_scan()
    assert lock is not None and lock.owner == "owner-1"

    later = JobLedger(store=store, clock=FixedClock(NOW + timedelta(seconds=30)))
    assert later.acquire_scan_lock("owner-2", ttl_seconds=10) is True
    assert later.release_scan_lock("owner-2") is True
    assert later.active_scan() is None


def test_release_requires_the_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    assert ledger.acquire_scan_lock("owner-1")
    assert ledger.release_scan_lock("someone-else") is False
    assert ledger.active_scan() is not None


def test_restart_reconstruction_reads_job_runs_and_scan_symbols(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    claim = ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-1")
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id

    store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=utc_timestamp(NOW),
            finished_at=utc_timestamp(NOW),
            error_code=None,
            report_id="RPT-1",
            data_quality="COMPLETE",
        )
    )
    store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="ETHUSDT",
            stage="METRICS",
            started_at=utc_timestamp(NOW),
            finished_at=None,
            error_code=None,
            report_id=None,
            data_quality=None,
        )
    )

    # A fresh ledger simulates a daemon restart: no in-memory state survives.
    restarted = JobLedger(store=store, clock=FixedClock(NOW + timedelta(seconds=30)))
    state = restarted.reconstruct("scan")

    assert isinstance(state, RestartState)
    assert [run.run_id for run in state.running] == [run_id]
    assert state.latest is not None and state.latest.run_id == run_id
    assert state.resumable_run_id == run_id
    assert [(item.scan_id, item.symbol, item.stage) for item in state.incomplete_scan_symbols] == [
        (run_id, "ETHUSDT", "METRICS")
    ]


def test_reconstruct_ignores_terminal_runs_for_resume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    claim = ledger.claim("scan", utc_timestamp(WINDOW), owner="owner-1")
    assert claim.claimed and claim.run is not None
    ledger.finish(claim.run.run_id, "DONE")

    state = JobLedger(store=store, clock=FixedClock(NOW)).reconstruct("scan")
    assert state.running == ()
    assert state.resumable_run_id is None


def test_latest_job_run_is_per_job_type(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    ledger.claim("scan", utc_timestamp(WINDOW), owner="a")
    ledger.claim("backfill", utc_timestamp(WINDOW), owner="b")

    assert ledger.latest("scan") is not None
    assert ledger.latest("scan").job_type == "scan"  # type: ignore[union-attr]
    assert ledger.latest("backfill") is not None
    assert ledger.latest("backfill").job_type == "backfill"  # type: ignore[union-attr]


def test_reclaimed_scheduled_run_resumes_only_incomplete_symbols(tmp_path: Path) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    clock = FixedClock(NOW)
    ledger = JobLedger(store=container.store, clock=clock)
    claim = ledger.claim("scan", utc_timestamp(WINDOW), owner="crashed", lease_seconds=10)
    assert claim.claimed and claim.run is not None
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=claim.run.run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=utc_timestamp(NOW),
            finished_at=utc_timestamp(NOW),
            error_code=None,
            report_id="RPT-1",
            data_quality="COMPLETE",
        )
    )

    later = FixedClock(NOW + timedelta(seconds=60))
    runner = ScheduledScanRunner(container=container, clock=later)
    outcome = runner.run_window(WINDOW, period="4h")

    assert outcome.claimed is True
    assert outcome.run is not None and outcome.run.run_id == claim.run.run_id
    assert outcome.scans == ()
    assert outcome.failed_symbols == ()
    assert container.store.get_job_run(claim.run.run_id).status == "DONE"  # type: ignore[union-attr]


def test_scheduled_scan_runner_refuses_a_duplicate_window(tmp_path: Path) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    first = runner.run_window(WINDOW, period="4h")
    second = runner.run_window(WINDOW, period="4h")

    assert first.claimed is True
    assert second.claimed is False
    assert second.reason == "duplicate"
    assert len(container.store.list_job_runs("scan")) == 1


def test_list_job_runs_returns_the_newest_window_after_sixty_one_runs(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ledger = JobLedger(store=store, clock=FixedClock(NOW))
    windows = [WINDOW + timedelta(hours=index) for index in range(61)]
    for index, window in enumerate(windows):
        assert ledger.claim("scan", utc_timestamp(window), owner=f"owner-{index}").claimed

    runs = store.list_job_runs("scan")
    state = ledger.reconstruct("scan")

    assert len(runs) == 50
    assert runs[0].window_start == utc_timestamp(windows[-1])
    assert state.latest is not None
    assert state.latest.window_start == utc_timestamp(windows[-1])
    assert state.resumable_run_id == runs[0].run_id


def test_restart_resumes_an_unexpired_running_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application.jobs import ScheduledScanOutcome
    from oft.application.scheduler import ScheduleState
    from oft.bootstrap import build_container
    from oft.cli.commands import daemon as daemon_module

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=container.clock)
    claim = ledger.claim("scan", "2026-11-07T08:00:00.000Z", owner="crashed", lease_seconds=900)
    assert claim.claimed and claim.run is not None

    resumed: list[str] = []
    monkeypatch.setattr(
        daemon_module.ScheduledScanRunner,
        "resume",
        lambda self, run_id, period="4h": (
            resumed.append(run_id) or ScheduledScanOutcome(run=None, claimed=True)
        ),
    )
    monkeypatch.setattr(
        daemon_module.ScheduledScanRunner,
        "run_window",
        lambda self, window, period="4h": ScheduledScanOutcome(run=None, claimed=False),
    )

    daemon_module._catch_up_on_start(container, ScheduleState(period="4h"))

    assert resumed == [claim.run.run_id]


def test_scheduled_runner_heartbeat_loop_renews_without_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.application.jobs as jobs_module
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))
    calls: list[str] = []

    def counting(self: JobLedger, run_id: str, *, owner: str, lease_seconds: int = 900) -> bool:
        calls.append(run_id)
        return True

    class OneTickStop:
        ticks = 0

        def wait(self, _timeout: float) -> bool:
            self.ticks += 1
            return self.ticks > 1

    monkeypatch.setattr(jobs_module, "HEARTBEAT_SECONDS", 1)
    monkeypatch.setattr(JobLedger, "heartbeat", counting)
    runner._heartbeat_loop("JOB-RUN", "owner", OneTickStop())  # type: ignore[arg-type]

    assert calls == ["JOB-RUN"]


def test_scheduled_runner_finishes_failed_on_an_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("execute exploded")

    monkeypatch.setattr(ScheduledScanRunner, "_execute", explode)
    with pytest.raises(RuntimeError, match="execute exploded"):
        runner.run_window(WINDOW, period="4h")

    run = container.store.get_job_run_by_window("scan", utc_timestamp(WINDOW))
    assert run is not None
    assert run.status == "FAILED"
    assert run.error_code == "JOB_FAILED"
    assert "execute exploded" in (run.message or "")


def test_scheduled_lock_contention_defers_instead_of_skipping(tmp_path: Path) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))
    assert ledger.acquire_scan_lock("other-owner")

    deferred = runner.run_window(WINDOW, period="4h")

    assert deferred.claimed is False
    assert deferred.reason == "scan_locked"
    assert container.store.get_job_run_by_window("scan", utc_timestamp(WINDOW)) is None

    assert ledger.release_scan_lock("other-owner")
    retried = runner.run_window(WINDOW, period="4h")
    assert retried.claimed is True


def test_scheduled_runner_marks_scans_as_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import oft.application.jobs as jobs_module
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    captured: list[object] = []

    class FakeScanService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self, request: object, *, scan_id: str | None = None) -> object:
            captured.append(request)
            return SimpleNamespace(
                scan=SimpleNamespace(finished_at=utc_timestamp(NOW)),
                report=SimpleNamespace(report_id="RPT-1", data_quality="COMPLETE"),
            )

    monkeypatch.setattr(jobs_module, "ScanService", FakeScanService)
    monkeypatch.setattr(
        container.store,
        "list_starred",
        lambda: (SimpleNamespace(symbol="BTCUSDT"),),
    )
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    outcome = runner.run_window(WINDOW, period="4h")

    assert outcome.claimed is True
    assert len(captured) == 1
    assert captured[0].trigger_source == "SCHEDULED"  # type: ignore[union-attr]


def test_catch_up_replans_after_crossing_a_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from oft.application.scheduler import ScheduleState, align_to_boundary
    from oft.cli.commands import daemon as daemon_module

    store = _store(tmp_path)
    start = NOW.replace(hour=7, minute=58, second=0)
    end = NOW.replace(hour=8, minute=2, second=0)

    class StepClock:
        def __init__(self) -> None:
            self.calls = 0

        def now(self) -> datetime:
            self.calls += 1
            return start if self.calls <= 4 else end

    clock = StepClock()
    ledger = JobLedger(store=store, clock=clock)  # type: ignore[arg-type]
    last = ledger.claim("scan", utc_timestamp(NOW.replace(hour=4)), owner="old", lease_seconds=900)
    assert last.run is not None
    ledger.finish(last.run.run_id, "DONE")

    container = SimpleNamespace(store=store, clock=clock, scan_lock=None)
    windows: list[datetime] = []
    monkeypatch.setattr(
        daemon_module.ScheduledScanRunner,
        "run_window",
        lambda self, window, period="4h": windows.append(window),
    )

    daemon_module._catch_up_on_start(container, ScheduleState(period="4h"))  # type: ignore[arg-type]

    assert windows == [align_to_boundary(end, "4h")]


def test_resume_without_checkpoints_reruns_the_scheduled_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from oft.application.jobs import ScheduledScanOutcome
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim("scan", utc_timestamp(WINDOW), owner="crashed", trigger_source="SCHEDULED")
    assert claim.claimed and claim.run is not None
    monkeypatch.setattr(
        container.store,
        "list_starred",
        lambda: (SimpleNamespace(symbol="BTCUSDT"),),
    )
    captured: list[tuple[str, ...]] = []

    def fake_execute(
        self: ScheduledScanRunner,
        run: object,
        symbols: tuple[str, ...],
        *,
        period: str,
        trigger_source: str = "SCHEDULED",
        **kwargs: object,
    ) -> ScheduledScanOutcome:
        captured.append(symbols)
        return ScheduledScanOutcome(run=None, claimed=True)

    monkeypatch.setattr(ScheduledScanRunner, "_execute", fake_execute)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    outcome = runner.resume(claim.run.run_id, period="4h")

    assert outcome.claimed is True
    assert captured == [("BTCUSDT",)]
    assert container.store.get_job_run(claim.run.run_id).status == "DONE"  # type: ignore[union-attr]


def test_resume_preserves_manual_source_metrics_only_and_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import oft.application.jobs as jobs_module
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL",
        owner="manual-owner",
        run_id="SCAN-OPTIONS",
        source="live",
        metrics_only=True,
        provider="stub",
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    assert claim.run.source == "live"
    assert claim.run.metrics_only is True
    assert claim.run.provider == "stub"
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=utc_timestamp(NOW),
            finished_at=utc_timestamp(NOW),
            error_code=None,
            report_id="RPT-BTC",
            data_quality="COMPLETE",
        )
    )
    monkeypatch.setattr(
        container.store,
        "list_starred",
        lambda: (SimpleNamespace(symbol="BTCUSDT"), SimpleNamespace(symbol="ETHUSDT")),
    )
    captured: list[object] = []

    class FakeScanService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self, request: object, *, scan_id: str | None = None) -> object:
            captured.append(request)
            return SimpleNamespace(
                scan=SimpleNamespace(finished_at=utc_timestamp(NOW)),
                report=SimpleNamespace(report_id="RPT-ETH", data_quality="COMPLETE"),
                symbol_stage="DONE",
                llm_tokens=0,
                cost_usd="0",
                cost_unpriced=False,
                llm_state={"configured": False},
            )

    monkeypatch.setattr(jobs_module, "ScanService", FakeScanService)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    outcome = runner.resume(run_id, period="4h")

    assert outcome.claimed is True
    assert len(captured) == 1
    request = captured[0]
    assert request.symbol == "ETHUSDT"  # type: ignore[union-attr]
    assert request.source == "live"  # type: ignore[union-attr]
    assert request.metrics_only is True  # type: ignore[union-attr]
    assert request.provider == "stub"  # type: ignore[union-attr]
    # metrics-only never reaches the LLM stage.
    assert container.store.list_llm_calls() == ()


def test_resume_runs_symbols_that_never_wrote_a_checkpoint_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from oft.application.jobs import ScheduledScanOutcome
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-MIDCRASH")
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=utc_timestamp(NOW),
            finished_at=utc_timestamp(NOW),
            error_code=None,
            report_id="RPT-BTC",
            data_quality="COMPLETE",
        )
    )
    monkeypatch.setattr(
        container.store,
        "list_starred",
        lambda: (SimpleNamespace(symbol="BTCUSDT"), SimpleNamespace(symbol="ETHUSDT")),
    )
    captured: list[tuple[str, ...]] = []

    def fake_execute(
        self: ScheduledScanRunner,
        run: object,
        symbols: tuple[str, ...],
        **kwargs: object,
    ) -> ScheduledScanOutcome:
        captured.append(symbols)
        return ScheduledScanOutcome(run=None, claimed=True)

    monkeypatch.setattr(ScheduledScanRunner, "_execute", fake_execute)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    outcome = runner.resume(run_id, period="4h")

    assert outcome.claimed is True
    assert captured == [("ETHUSDT",)]
    assert container.store.get_job_run(run_id).status == "DONE"  # type: ignore[union-attr]


def test_resume_with_every_row_terminal_finishes_without_rerunning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from oft.application.jobs import ScheduledScanOutcome
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-ALLDONE")
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    for symbol in ("BTCUSDT", "ETHUSDT"):
        container.store.upsert_scan_symbol(
            ScanSymbolRecord(
                scan_id=run_id,
                symbol=symbol,
                stage="DONE",
                started_at=utc_timestamp(NOW),
                finished_at=utc_timestamp(NOW),
                error_code=None,
                report_id=f"RPT-{symbol}",
                data_quality="COMPLETE",
            )
        )
    monkeypatch.setattr(
        container.store,
        "list_starred",
        lambda: (SimpleNamespace(symbol="BTCUSDT"), SimpleNamespace(symbol="ETHUSDT")),
    )
    captured: list[tuple[str, ...]] = []

    def fake_execute(
        self: ScheduledScanRunner,
        run: object,
        symbols: tuple[str, ...],
        **kwargs: object,
    ) -> ScheduledScanOutcome:
        captured.append(symbols)
        return ScheduledScanOutcome(run=None, claimed=True)

    monkeypatch.setattr(ScheduledScanRunner, "_execute", fake_execute)
    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))

    outcome = runner.resume(run_id, period="4h")

    assert outcome.claimed is True
    assert captured == []
    assert container.store.get_job_run(run_id).status == "DONE"  # type: ignore[union-attr]


def test_resume_manual_run_without_checkpoints_is_failed_not_done(tmp_path: Path) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-1")
    assert claim.claimed and claim.run is not None

    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW))
    outcome = runner.resume(claim.run.run_id, period="4h")

    assert outcome.claimed is False
    assert container.store.get_job_run(claim.run.run_id).status == "FAILED"  # type: ignore[union-attr]
    assert container.store.get_job_run(claim.run.run_id).error_code == "MANUAL_RUN_INTERRUPTED"  # type: ignore[union-attr]


def test_reclaimed_run_losing_the_lock_keeps_its_audit_row(tmp_path: Path) -> None:
    from oft.bootstrap import build_container

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    early = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = early.claim(
        "scan", utc_timestamp(WINDOW), owner="crashed", trigger_source="SCHEDULED", lease_seconds=10
    )
    assert claim.claimed and claim.run is not None
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=claim.run.run_id,
            symbol="BTCUSDT",
            stage="DATA",
            started_at=utc_timestamp(NOW),
            finished_at=None,
            error_code=None,
            report_id=None,
            data_quality=None,
        )
    )
    later = JobLedger(store=container.store, clock=FixedClock(NOW + timedelta(seconds=60)))
    assert later.acquire_scan_lock("other-owner")

    runner = ScheduledScanRunner(container=container, clock=FixedClock(NOW + timedelta(seconds=60)))
    outcome = runner.run_window(WINDOW, period="4h")

    assert outcome.claimed is False
    run = container.store.get_job_run(claim.run.run_id)
    assert run is not None
    assert run.status == "FAILED"
    assert run.error_code == "SCAN_ALREADY_RUNNING"
    assert container.store.list_scan_symbols(claim.run.run_id)
