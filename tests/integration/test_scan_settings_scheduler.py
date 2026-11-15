"""Persisted ``scan.period`` pickup by the daemon scheduler (slice 32)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import database_path
from oft.application.scheduler import (
    ScanDaemonScheduler,
    ScheduleState,
    SettingsPeriodSync,
    next_boundary,
    save_schedule_state,
    schedule_state_path,
)
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.cli.commands.daemon import _schedule_state
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.settings import SCAN_PERIOD_KEY
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
TRANSITION = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class MutableClock:
    """A clock the test advances across a UTC boundary."""

    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


@pytest.fixture(autouse=True)
def _clear_scan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _corrupt_record() -> ConfigRecord:
    return ConfigRecord(
        key=SCAN_PERIOD_KEY,
        value_json="not-json",
        updated_at="2026-11-06T10:00:00.000Z",
        updated_by="web",
    )


def test_a_persisted_save_is_pending_until_the_next_boundary_and_keeps_the_window(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=MutableClock(NOW))
    SettingsService(
        store=container.store,
        clock=container.clock,
    ).update_scan_period("8h", actor="cli")

    read_only = build_container(settings, clock=MutableClock(NOW))
    state = _schedule_state(settings, container=read_only, now=NOW)

    # The current 4h window is never rewritten.
    assert state.period_at(NOW) == "4h"
    assert state.period_at(TRANSITION.replace(hour=11)) == "4h"
    assert state.pending_period == "8h"
    assert state.effective_from == TRANSITION
    # The change applies at the boundary without a restart.
    assert state.period_at(TRANSITION) == "8h"
    assert state.period_at(TRANSITION.replace(hour=16)) == "8h"
    assert next_boundary(NOW, state.period_at(NOW)) == TRANSITION


def test_startup_preserves_an_already_pending_period(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=MutableClock(NOW))
    container.store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("8h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )
    save_schedule_state(
        schedule_state_path(settings),
        ScheduleState(period="4h", pending_period="8h", effective_from=TRANSITION),
    )

    state = _schedule_state(settings, container=build_container(settings), now=NOW)

    assert state.period == "4h"
    assert state.pending_period == "8h"
    assert state.effective_from == TRANSITION


def test_a_persisted_value_equal_to_the_active_period_adds_no_pending_change(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=MutableClock(NOW))
    container.store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("4h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )

    state = _schedule_state(settings, container=build_container(settings), now=NOW)

    assert state.period == "4h"
    assert state.pending_period is None


def test_refresh_period_only_requests_a_change_once_and_applies_at_the_boundary() -> None:
    clock = MutableClock(NOW)
    scheduler = ScanDaemonScheduler(
        clock=clock,
        state=ScheduleState(period="4h"),
        job=lambda _window: None,
    )

    assert scheduler.refresh_period("8h") is True
    assert scheduler.state.period_at(NOW) == "4h"
    assert scheduler.state.pending_period == "8h"
    assert scheduler.state.effective_from == TRANSITION

    # A second sync for the same persisted value is a no-op (no extra boundary
    # shift), and the same holds while the change is pending.
    assert scheduler.refresh_period("8h") is False

    # Reverting the persisted value before the boundary collapses the pending
    # change; the active 4h window is untouched.
    assert scheduler.refresh_period("4h") is True
    assert scheduler.state.pending_period is None
    assert scheduler.state.period_at(NOW) == "4h"

    # Saving 8h again sets a fresh pending change that applies at the boundary.
    assert scheduler.refresh_period("8h") is True
    clock.instant = TRANSITION.replace(minute=1)
    assert scheduler.state.period_at(clock.now()) == "8h"
    assert scheduler.next_run().hour == 16

    scheduler.shutdown()


def test_settings_period_sync_ignores_an_absent_persisted_value() -> None:
    scheduler = ScanDaemonScheduler(
        clock=MutableClock(NOW),
        state=ScheduleState(period="4h"),
        job=lambda _window: None,
    )
    sync = SettingsPeriodSync(scheduler=scheduler, period_provider=lambda: None)

    assert sync.sync() is False
    assert scheduler.state.period_at(NOW) == "4h"
    assert scheduler.state.pending_period is None

    sync = SettingsPeriodSync(scheduler=scheduler, period_provider=lambda: "8h")
    assert sync.sync() is True
    assert sync.sync() is False

    scheduler.shutdown()


def test_startup_collapses_a_stale_pending_change_after_a_revert(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=MutableClock(NOW))
    # The operator reverted to 4h while the daemon was down, but schedule.json
    # still carries the old pending 8h change.
    container.store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("4h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )
    save_schedule_state(
        schedule_state_path(settings),
        ScheduleState(period="4h", pending_period="8h", effective_from=TRANSITION),
    )

    state = _schedule_state(settings, container=build_container(settings), now=NOW)

    assert state.period == "4h"
    assert state.pending_period is None
    assert state.effective_from is None


def test_a_pre_v26_database_without_the_config_table_does_not_crash_diagnostics(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    database_path(settings).write_bytes(b"")

    state = _schedule_state(settings, container=build_container(settings), now=NOW)

    assert state.period == "4h"
    assert state.pending_period is None


def test_next_run_is_for_the_effective_period_while_the_daemon_applies_later(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    # The active schedule is 8h; the operator saves 4h at 10:00.
    save_schedule_state(schedule_state_path(settings), ScheduleState(period="8h"))
    container.store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("4h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )

    api = SettingsService(store=container.store, clock=FixedClock(NOW)).read_scan_settings()
    state = _schedule_state(
        settings, container=build_container(settings, clock=FixedClock(NOW)), now=NOW
    )

    # ``next_run`` is the next boundary of the effective 4h period (12:00Z),
    # while the daemon applies the change at the active 8h boundary (16:00Z).
    assert api.next_run == "2026-11-06T12:00:00.000Z"
    assert state.period_at(NOW) == "8h"
    assert state.effective_from == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert state.period_at(state.effective_from) == "4h"
