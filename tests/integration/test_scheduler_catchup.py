"""Catch-up policy, daemon lifecycle CLI, and scan schedule status."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.application.jobs import JobLedger
from oft.application.scheduler import plan_catch_up
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp

DAY = datetime(2026, 11, 7, tzinfo=UTC)
NOW = DAY.replace(hour=13, minute=5)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _ledger(tmp_path: Path, *, now: datetime = NOW) -> tuple[SqliteStore, JobLedger]:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    return store, JobLedger(store=store, clock=FixedClock(now))


def test_catch_up_records_older_windows_as_missed(tmp_path: Path) -> None:
    store, ledger = _ledger(tmp_path)
    plan = ledger.catch_up(
        period="4h",
        now=NOW,
        last_attempted_window_start=DAY,
    )

    assert plan.run_window == DAY.replace(hour=12)
    runs = {run.window_start: run for run in store.list_job_runs("scan")}
    assert runs[utc_timestamp(DAY.replace(hour=4))].status == "MISSED"
    assert runs[utc_timestamp(DAY.replace(hour=8))].status == "MISSED"
    assert utc_timestamp(DAY.replace(hour=12)) not in runs
    assert "latest" in (runs[utc_timestamp(DAY.replace(hour=8))].message or "")


def test_catch_up_is_idempotent_on_repeated_startup(tmp_path: Path) -> None:
    store, ledger = _ledger(tmp_path)
    ledger.catch_up(period="4h", now=NOW, last_attempted_window_start=DAY)
    ledger.catch_up(period="4h", now=NOW, last_attempted_window_start=DAY)

    versions = [run.window_start for run in store.list_job_runs("scan")]
    assert len(versions) == len(set(versions)) == 2


def test_latest_missed_window_can_then_be_claimed(tmp_path: Path) -> None:
    store, ledger = _ledger(tmp_path)
    plan = ledger.catch_up(period="4h", now=NOW, last_attempted_window_start=DAY)
    assert plan.run_window is not None

    claim = ledger.claim(
        "scan", utc_timestamp(plan.run_window), owner="owner-1", trigger_source="SCHEDULED"
    )
    assert claim.claimed is True
    assert {run.status for run in store.list_job_runs("scan")} == {"MISSED", "RUNNING"}


def test_catch_up_uses_eight_hour_boundaries(tmp_path: Path) -> None:
    now = DAY.replace(hour=17, minute=30)
    store, ledger = _ledger(tmp_path, now=now)
    plan = plan_catch_up(
        now=now,
        period="8h",
        last_attempted_window_start=DAY,
    )
    assert plan.run_window == DAY.replace(hour=16)
    assert [missed.window_start for missed in plan.missed] == [DAY.replace(hour=8)]


# -- daemon lifecycle CLI --------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_SCAN_PERIOD", "4h")


def test_daemon_status_offline_does_not_require_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)

    result = CliRunner().invoke(cli_app, ["daemon", "status"])
    assert result.exit_code == 0, result.output
    assert "OFFLINE" in result.output


def test_daemon_status_reads_the_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    run_dir = settings.data_dir / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")

    result = CliRunner().invoke(cli_app, ["daemon", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["state"] == "RUNNING"
    assert payload["data"]["pid"] == os.getpid()


def test_daemon_logs_prints_the_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = CliRunner().invoke(cli_app, ["daemon", "logs", "--lines", "2"])
    assert result.exit_code == 0, result.output
    assert "two" in result.output and "three" in result.output
    assert "one" not in result.output


def test_daemon_logs_when_missing_is_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    result = CliRunner().invoke(cli_app, ["daemon", "logs"])
    assert result.exit_code == 0, result.output
    assert "no daemon log" in result.output.lower()


def test_daemon_stop_when_not_running_is_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    result = CliRunner().invoke(cli_app, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert "not running" in result.output.lower()


def test_scan_status_shows_next_and_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    store = SqliteStore(settings)
    store.initialize()
    clock = FixedClock(datetime(2026, 11, 7, 9, 30, tzinfo=UTC))
    ledger = JobLedger(store=store, clock=clock)
    claim = ledger.claim(
        "scan",
        utc_timestamp(datetime(2026, 11, 7, 8, tzinfo=UTC)),
        owner="owner-1",
        trigger_source="SCHEDULED",
    )
    assert claim.claimed and claim.run is not None
    ledger.finish(claim.run.run_id, "DONE")

    result = CliRunner().invoke(cli_app, ["scan", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    data = payload["data"]
    assert data["timezone"] == "UTC"
    assert data["period"] == "4h"
    assert data["next_run_at"].endswith("Z")
    assert data["last_run"]["run_id"] == claim.run.run_id
    assert data["last_run"]["status"] == "DONE"

    table = CliRunner().invoke(cli_app, ["scan", "status"])
    assert table.exit_code == 0, table.output
    assert "Next run (UTC)" in table.output
    assert "Last run (UTC)" in table.output


def test_scan_status_without_history_reports_never(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)

    result = CliRunner().invoke(cli_app, ["scan", "status"])
    assert result.exit_code == 0, result.output
    assert "never" in result.output


def test_configured_logging_writes_a_tailable_daemon_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import structlog

    from oft.cli.commands.daemon import log_path
    from oft.logging import configure_logging

    settings = _settings(tmp_path)
    _cli_env(monkeypatch, settings)
    configure_logging(settings)
    structlog.get_logger("test").info("daemon_log_probe", answer=42)
    path = log_path(settings)

    assert path.is_file()
    assert "daemon_log_probe" in path.read_text(encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["daemon", "logs", "--lines", "5"])
    assert result.exit_code == 0, result.output
    assert "daemon_log_probe" in result.output
