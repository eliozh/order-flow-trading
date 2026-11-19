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
from oft.application.settings_service import (
    SettingsService,
    resolve_schedule_state_overlay,
)
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
    # The running daemon has an active 4h window recorded before the save.
    save_schedule_state(schedule_state_path(settings), ScheduleState(period="4h"))
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


# -- H1 / M1: a due pending change is promoted, never shown as still pending ----


def _pending_8h() -> ScheduleState:
    return ScheduleState(period="4h", pending_period="8h", effective_from=TRANSITION)


def test_a_due_pending_change_is_promoted_after_the_boundary() -> None:
    state = _pending_8h()
    assert state.period_at(NOW) == "4h"

    promoted = state.promote(TRANSITION.replace(minute=1))

    assert promoted is not state
    assert promoted.period == "8h"
    assert promoted.pending_period is None
    assert promoted.effective_from is None
    assert promoted.period_at(TRANSITION.replace(minute=1)) == "8h"


def test_revert_after_apply_requests_a_new_pending_change_at_the_new_boundary() -> None:
    state = _pending_8h()
    applied = TRANSITION.replace(minute=1)

    promoted, changed = state.apply_persisted("4h", now=applied)

    assert changed is True
    assert promoted.period_at(applied) == "8h"
    assert promoted.period_at(TRANSITION.replace(hour=15)) == "8h"
    assert promoted.pending_period == "4h"
    assert promoted.effective_from == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert promoted.period_at(datetime(2026, 11, 6, 16, 0, tzinfo=UTC)) == "4h"
    # A revert is not treated as an immediate no-op against the stale pending.


def test_reapplying_the_now_active_period_after_the_boundary_is_a_clean_no_op() -> None:
    state = _pending_8h()
    applied = TRANSITION.replace(minute=1)

    promoted, changed = state.apply_persisted("8h", now=applied)

    assert changed is True
    assert promoted.period == "8h"
    assert promoted.pending_period is None
    assert promoted.effective_from is None


def test_status_payload_after_the_boundary_shows_the_applied_period_and_no_pending() -> None:
    state = _pending_8h()

    payload = state.next_run_payload(TRANSITION.replace(minute=1))

    assert payload["period"] == "8h"
    assert payload["pending_period"] == ""
    assert payload["next_run_at"].startswith("2026-11-06T16:00:")


def test_schedule_state_clears_a_stale_pending_value_once_the_boundary_passed(
    tmp_path: Path,
) -> None:
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
    save_schedule_state(schedule_state_path(settings), _pending_8h())
    applied = MutableClock(TRANSITION.replace(minute=1))

    state = _schedule_state(settings, container=container, now=applied.now())

    assert state.period_at(applied.now()) == "8h"
    assert state.pending_period is None
    assert state.effective_from is None


def test_the_scheduler_and_domain_agree_on_periods_and_boundaries() -> None:
    from oft.application.scheduler import BOUNDARY_HOURS, PERIOD_HOURS
    from oft.domain.settings import SCAN_PERIOD_HOURS, SUPPORTED_SCAN_PERIODS

    assert tuple(SCAN_PERIOD_HOURS) == SUPPORTED_SCAN_PERIODS
    assert dict(SCAN_PERIOD_HOURS) == PERIOD_HOURS
    for period, hours in SCAN_PERIOD_HOURS.items():
        assert BOUNDARY_HOURS[period] == tuple(range(0, 24, hours))


# -- M1: the overlay honors a cadence set only in ``.env`` ---------------------


def test_overlay_honors_a_dotenv_cadence_without_a_persisted_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cadence set only in ``.env`` must reach the status overlay (M1)."""
    (tmp_path / ".env").write_text(
        "OFT_SCAN_PERIOD=8h\nOFT_SCAN_JITTER_SECONDS=99\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)

    state = resolve_schedule_state_overlay(
        settings=settings,
        store=None,
        clock=FixedClock(NOW),
    )

    assert state.period == "8h"
    assert state.jitter_seconds_max == 99


def test_overlay_lets_a_persisted_period_win_over_an_invalid_dotenv_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: a saved 8h must not fail because ``.env`` holds an invalid period."""
    (tmp_path / ".env").write_text(
        "OFT_SCAN_PERIOD=1h\nOFT_SCAN_JITTER_SECONDS=30\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps("8h"),
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )

    state = resolve_schedule_state_overlay(
        settings=settings,
        store=container.store,
        clock=FixedClock(NOW),
    )

    assert state.period == "8h"
    assert state.jitter_seconds_max == 30


# -- M4/M8: the daemon sync seam never raises out of the interval job ----------


def test_run_settings_sync_swallows_value_and_config_errors_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.cli.commands import daemon as daemon_module
    from oft.config import ConfigError

    warnings: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        def warning(self, event: str, **fields: object) -> None:
            warnings.append((event, fields))

    # Order-independent: structlog's ``cache_logger_on_first_use`` may already
    # have cached a stdlib logger after another test ran ``configure_logging``.
    monkeypatch.setattr(daemon_module, "_LOGGER", _Recorder())

    class _Raising:
        def __init__(self, error: BaseException) -> None:
            self._error = error

        def sync(self) -> bool:
            raise self._error

    assert daemon_module._run_settings_sync(_Raising(ValueError("bad jitter"))) is False
    assert len(warnings) == 1
    assert warnings[0][0] == "scan_period_sync_failed"
    assert "bad jitter" in str(warnings[0][1].get("error", ""))

    warnings.clear()
    assert daemon_module._run_settings_sync(_Raising(ConfigError("corrupt row"))) is False
    assert len(warnings) == 1
    assert warnings[0][0] == "scan_period_sync_failed"
    assert "corrupt row" in str(warnings[0][1].get("error", ""))


def test_run_settings_sync_returns_the_scheduler_change_flag() -> None:
    from oft.cli.commands.daemon import _run_settings_sync

    class _Sync:
        def __init__(self, changed: bool) -> None:
            self._changed = changed

        def sync(self) -> bool:
            return self._changed

    assert _run_settings_sync(_Sync(True)) is True
    assert _run_settings_sync(_Sync(False)) is False
