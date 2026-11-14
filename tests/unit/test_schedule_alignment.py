"""UTC 4h/8h boundary alignment, jitter, and period-change semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oft.application.scheduler import (
    BOUNDARY_HOURS,
    MANAGEMENT_JOB_ID,
    POSITION_MONITOR_JOB_ID,
    CatchUpPlan,
    ManagementDaemonScheduler,
    MissedWindow,
    PositionMonitorDaemonScheduler,
    ScheduleState,
    align_to_boundary,
    boundary_hours,
    is_boundary,
    jitter_seconds,
    latest_due_window,
    next_boundary,
    plan_catch_up,
    run_at,
    validate_period,
    window_for,
)

DAY = datetime(2026, 11, 7, tzinfo=UTC)


def test_four_hour_boundaries_are_utc_anchor_hours() -> None:
    assert BOUNDARY_HOURS["4h"] == (0, 4, 8, 12, 16, 20)
    assert boundary_hours("4h") == (0, 4, 8, 12, 16, 20)


def test_eight_hour_boundaries_are_utc_anchor_hours() -> None:
    assert BOUNDARY_HOURS["8h"] == (0, 8, 16)
    assert boundary_hours("8h") == (0, 8, 16)


def test_only_4h_and_8h_are_supported() -> None:
    assert validate_period("4h") == "4h"
    assert validate_period("8h") == "8h"
    with pytest.raises(ValueError):
        validate_period("1h")


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
def test_supported_hours_are_boundaries(hour: int) -> None:
    assert is_boundary(DAY.replace(hour=hour), "4h")
    assert not is_boundary(DAY.replace(hour=hour, minute=1), "4h")


def test_align_flooris_to_the_utc_boundary() -> None:
    instant = DAY.replace(hour=13, minute=37, second=12, microsecond=500_000)
    assert align_to_boundary(instant, "4h") == DAY.replace(hour=12)
    assert align_to_boundary(instant, "8h") == DAY.replace(hour=8)


def test_align_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError):
        align_to_boundary(datetime(2026, 11, 7, 13), "4h")


def test_align_preserves_utc_awareness() -> None:
    aligned = align_to_boundary(DAY.replace(hour=13), "8h")
    assert aligned.tzinfo is UTC


def test_next_boundary_is_strictly_after_the_instant() -> None:
    assert next_boundary(DAY.replace(hour=5, minute=30), "4h") == DAY.replace(hour=8)
    assert next_boundary(DAY.replace(hour=5, minute=30), "8h") == DAY.replace(hour=8)
    # Exactly on a boundary, the next one is a full period later.
    assert next_boundary(DAY.replace(hour=8), "4h") == DAY.replace(hour=12)
    assert next_boundary(DAY.replace(hour=8), "8h") == DAY.replace(hour=16)


def test_next_boundary_crosses_midnight_in_utc() -> None:
    instant = DAY.replace(hour=23, minute=15)
    assert next_boundary(instant, "4h") == DAY + timedelta(days=1)
    assert next_boundary(instant, "8h") == DAY + timedelta(days=1)


def test_window_for_is_half_open() -> None:
    start, end = window_for(DAY.replace(hour=13, minute=5), "4h")
    assert start == DAY.replace(hour=12)
    assert end == DAY.replace(hour=16)


def test_jitter_is_deterministic_and_bounded() -> None:
    window = DAY.replace(hour=8)
    first = jitter_seconds("4h", window, max_jitter=30)
    second = jitter_seconds("4h", window, max_jitter=30)
    assert first == second
    assert 0 <= first < 30


def test_jitter_differs_across_windows_and_is_never_negative() -> None:
    values = {
        jitter_seconds("4h", DAY.replace(hour=hour), max_jitter=30)
        for hour in (0, 4, 8, 12, 16, 20)
    }
    assert len(values) > 1
    assert all(value >= 0 for value in values)


def test_run_at_is_boundary_plus_jitter() -> None:
    window = DAY.replace(hour=8)
    scheduled = run_at(window, "4h", max_jitter=30)
    assert scheduled >= window
    assert scheduled < window + timedelta(seconds=30)
    assert scheduled == window + timedelta(seconds=jitter_seconds("4h", window))


def test_latest_due_window_is_the_floor_of_now() -> None:
    assert latest_due_window(DAY.replace(hour=13), "4h") == DAY.replace(hour=12)
    assert latest_due_window(DAY.replace(hour=8), "8h") == DAY.replace(hour=8)


def test_period_change_takes_effect_at_the_next_boundary() -> None:
    state = ScheduleState(period="4h")
    now = DAY.replace(hour=5, minute=30)
    changed = state.request_period("8h", now=now)

    # Before the next boundary the active period is still 4h.
    assert changed.period_at(now) == "4h"
    assert changed.period_at(DAY.replace(hour=7, minute=59)) == "4h"
    # From 08:00 UTC the pending period is active.
    assert changed.period_at(DAY.replace(hour=8)) == "8h"
    assert changed.period_at(DAY.replace(hour=9)) == "8h"


def test_period_change_reschedules_the_next_run_to_the_transition_boundary() -> None:
    state = ScheduleState(period="4h")
    now = DAY.replace(hour=5, minute=30)
    changed = state.request_period("8h", now=now)
    next_run = changed.next_run(now)
    assert next_run >= DAY.replace(hour=8)
    assert next_run < DAY.replace(hour=8) + timedelta(seconds=30)


def test_next_run_is_utc_and_after_now() -> None:
    now = DAY.replace(hour=9, minute=30)
    scheduled = ScheduleState(period="4h").next_run(now)
    assert scheduled.tzinfo is UTC
    assert scheduled > now


def test_catch_up_runs_only_the_latest_missed_window() -> None:
    now = DAY.replace(hour=13, minute=5)
    plan = plan_catch_up(
        now=now,
        period="4h",
        last_attempted_window_start=DAY,
    )
    assert isinstance(plan, CatchUpPlan)
    assert plan.run_window == DAY.replace(hour=12)
    assert [missed.window_start for missed in plan.missed] == [
        DAY.replace(hour=4),
        DAY.replace(hour=8),
    ]
    assert all(isinstance(missed, MissedWindow) for missed in plan.missed)
    assert all("latest" in missed.reason for missed in plan.missed)


def test_catch_up_with_nothing_missed_does_nothing() -> None:
    plan = plan_catch_up(
        now=DAY.replace(hour=8, minute=5),
        period="4h",
        last_attempted_window_start=DAY.replace(hour=8),
    )
    assert plan.run_window is None
    assert plan.missed == ()


def test_catch_up_on_first_start_runs_the_due_window_without_history() -> None:
    plan = plan_catch_up(
        now=DAY.replace(hour=13),
        period="4h",
        last_attempted_window_start=None,
    )
    assert plan.run_window == DAY.replace(hour=12)
    assert plan.missed == ()


def test_catch_up_skips_a_window_that_already_has_a_run() -> None:
    plan = plan_catch_up(
        now=DAY.replace(hour=13),
        period="4h",
        last_attempted_window_start=DAY,
        completed_window_starts=(DAY.replace(hour=12),),
    )
    assert plan.run_window is None
    assert [missed.window_start for missed in plan.missed] == [
        DAY.replace(hour=4),
        DAY.replace(hour=8),
    ]


def test_catch_up_uses_eight_hour_boundaries() -> None:
    now = DAY.replace(hour=17, minute=30)
    plan = plan_catch_up(
        now=now,
        period="8h",
        last_attempted_window_start=DAY,
    )
    assert plan.run_window == DAY.replace(hour=16)
    assert [missed.window_start for missed in plan.missed] == [DAY.replace(hour=8)]


def test_no_history_catch_up_respects_the_completed_set() -> None:
    due = latest_due_window(DAY.replace(hour=13), "4h")
    plan = plan_catch_up(
        now=DAY.replace(hour=13),
        period="4h",
        last_attempted_window_start=None,
        completed_window_starts=(due,),
    )
    assert plan.run_window is None


def test_jitter_must_be_shorter_than_the_period() -> None:
    from oft.application.scheduler import resolve_schedule_settings

    with pytest.raises(ValueError, match="less than the scan period"):
        resolve_schedule_settings(
            {"OFT_SCAN_PERIOD": "4h", "OFT_SCAN_JITTER_SECONDS": str(4 * 3600)}
        )
    with pytest.raises(ValueError, match="less than the scan period"):
        ScheduleState(period="4h", jitter_seconds_max=4 * 3600)


def test_date_trigger_has_no_misfire_cap_so_fire_always_reschedules() -> None:
    from oft.application.scheduler import ScanDaemonScheduler
    from oft.clock import FixedClock

    scheduler = ScanDaemonScheduler(
        clock=FixedClock(DAY),
        state=ScheduleState(period="4h"),
        job=lambda _window: None,
    )
    scheduler._schedule_next()
    job = scheduler.scheduler.get_job("scan")
    assert job is not None
    assert job.misfire_grace_time is None
    scheduler.shutdown()


def test_pending_period_change_is_persisted_and_reloaded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from oft.application.scheduler import (
        ScanDaemonScheduler,
        load_schedule_state,
        save_schedule_state,
    )
    from oft.clock import FixedClock

    path = tmp_path / "schedule.json"
    scheduler = ScanDaemonScheduler(
        clock=FixedClock(DAY),
        state=ScheduleState(period="4h"),
        job=lambda _window: None,
        state_path=path,
    )
    scheduler.request_period("8h", now=DAY)
    loaded = load_schedule_state(path, default=ScheduleState(period="4h"))
    assert loaded.pending_period == "8h"
    assert loaded.period_at(DAY + timedelta(hours=4)) == "8h"

    save_schedule_state(path, ScheduleState(period="8h"))
    assert load_schedule_state(path, default=ScheduleState(period="4h")).period == "8h"


def test_jitter_uses_the_shorter_of_active_and_pending_periods() -> None:
    with pytest.raises(ValueError, match="less than the scan period"):
        ScheduleState(
            period="8h",
            pending_period="4h",
            effective_from=DAY + timedelta(hours=8),
            jitter_seconds_max=8 * 3600,
        )


def test_fire_records_skipped_windows_and_runs_the_latest_due_window() -> None:
    from oft.application.scheduler import ScanDaemonScheduler

    class LateClock:
        instant = DAY.replace(hour=12, minute=5)

        def now(self) -> datetime:
            return self.instant

    clock = LateClock()
    jobs: list[datetime] = []
    missed: list[datetime] = []
    scheduler = ScanDaemonScheduler(
        clock=clock,  # type: ignore[arg-type]
        state=ScheduleState(period="4h"),
        job=jobs.append,
        on_missed=missed.append,
    )
    scheduler._due_window = DAY.replace(hour=8)
    scheduler._fire()

    assert jobs == [DAY.replace(hour=12)]
    assert missed == [DAY.replace(hour=8)]
    scheduler.shutdown()


# -- slice 26: position-monitor interval and management cadence ---------------


def test_position_monitor_interval_config_bounds_and_default() -> None:
    from pydantic import ValidationError

    from oft.config import CONFIG_INVALID, ConfigError, Settings, load_settings

    assert Settings().position_monitor_interval_seconds == 15
    assert Settings(position_monitor_interval_seconds=5).position_monitor_interval_seconds == 5
    assert Settings(position_monitor_interval_seconds=60).position_monitor_interval_seconds == 60
    with pytest.raises(ValidationError):
        Settings(position_monitor_interval_seconds=4)
    with pytest.raises(ValidationError):
        Settings(position_monitor_interval_seconds=61)
    with pytest.raises(ConfigError) as error:
        load_settings(position_monitor_interval_seconds=0)
    assert error.value.code == CONFIG_INVALID


def test_management_cadence_override_resolution() -> None:
    from oft.config import CONFIG_INVALID, ConfigError, Settings, load_settings

    assert Settings().management_cadence is None
    assert Settings(management_cadence="8h").management_cadence == "8h"
    # The value is normalized against the supported scan periods.
    assert Settings(management_cadence=" 4H ").management_cadence == "4h"
    assert Settings(management_cadence="").management_cadence is None
    with pytest.raises(ConfigError) as error:
        load_settings(management_cadence="1h")
    assert error.value.code == CONFIG_INVALID


def test_management_condition_toggles_default_true() -> None:
    from oft.config import Settings

    defaults = Settings()
    assert defaults.management_funding_settlement_enabled is True
    assert defaults.management_risk_breach_enabled is True
    disabled = Settings(
        management_funding_settlement_enabled=False,
        management_risk_breach_enabled=False,
    )
    assert disabled.management_funding_settlement_enabled is False
    assert disabled.management_risk_breach_enabled is False


def test_management_window_alignment_uses_the_scan_boundary() -> None:
    """A management window is the half-open scan window containing the instant."""
    instant = DAY.replace(hour=13, minute=5)
    start, end = window_for(instant, "4h")
    assert (start, end) == (DAY.replace(hour=12), DAY.replace(hour=16))
    start8, end8 = window_for(instant, "8h")
    assert (start8, end8) == (DAY.replace(hour=8), DAY.replace(hour=16))


def test_position_monitor_scheduler_is_single_instance_with_coalescing() -> None:
    scheduler = PositionMonitorDaemonScheduler(
        interval_seconds=15,
        job=lambda: None,
    )

    job = scheduler.scheduler.get_job(POSITION_MONITOR_JOB_ID)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.trigger.interval.total_seconds() == 15
    assert job.misfire_grace_time == 15
    scheduler.shutdown()  # no-op when not running


def test_management_scheduler_fires_at_the_next_scan_boundary() -> None:
    from oft.clock import FixedClock

    now = DAY.replace(hour=5, minute=30)
    jobs: list[datetime] = []
    scheduler = ManagementDaemonScheduler(
        clock=FixedClock(now),
        job=jobs.append,
        period_provider=lambda: "4h",
    )
    scheduler._schedule_next()

    expected = run_at(next_boundary(now, "4h"), "4h")
    job = scheduler.scheduler.get_job(MANAGEMENT_JOB_ID)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert scheduler.next_run() == expected
    run_date = job.trigger.run_date
    assert abs(run_date.timestamp() - expected.timestamp()) < 1
    scheduler.shutdown()


def test_management_scheduler_picks_up_a_scan_period_change() -> None:
    from oft.clock import FixedClock

    now = DAY.replace(hour=2, minute=30)
    period = {"value": "4h"}
    scheduler = ManagementDaemonScheduler(
        clock=FixedClock(now),
        job=lambda _window: None,
        period_provider=lambda: period["value"],
    )
    assert scheduler.next_run() == run_at(next_boundary(now, "4h"), "4h")

    period["value"] = "8h"
    assert scheduler.next_run() == run_at(next_boundary(now, "8h"), "8h")
    scheduler.shutdown()


def test_management_scheduler_never_catches_up_after_a_late_fire() -> None:
    class _MutableClock:
        def __init__(self, instant: datetime) -> None:
            self.instant = instant

        def now(self) -> datetime:
            return self.instant

    clock = _MutableClock(DAY.replace(hour=5, minute=30))
    jobs: list[datetime] = []
    scheduler = ManagementDaemonScheduler(
        clock=clock,
        job=jobs.append,
        period_provider=lambda: "4h",
    )
    scheduler._schedule_next()

    # The 08:00 window was missed; a late fire runs only the current window.
    clock.instant = DAY.replace(hour=12, minute=5)
    scheduler._fire()

    assert jobs == [DAY.replace(hour=12)]
    expected_next = run_at(next_boundary(clock.instant, "4h"), "4h")
    assert scheduler.next_run() == expected_next
    scheduler.shutdown()


def test_management_scheduler_rearms_at_a_pending_period_change_boundary() -> None:
    """A pending 8h -> 4h change effective at 04:00 must not skip 04:00."""
    from oft.clock import FixedClock

    now = DAY.replace(hour=3)
    state = ScheduleState(
        period="8h",
        pending_period="4h",
        effective_from=DAY.replace(hour=4),
        jitter_seconds_max=0,
    )
    scheduler = ManagementDaemonScheduler(
        clock=FixedClock(now),
        job=lambda _window: None,
        # The old period-only path reports 8h here and computes 08:00.
        period_provider=lambda: "8h",
        state_provider=lambda: state,
    )

    assert scheduler.next_run() == DAY.replace(hour=4)
    assert scheduler.next_run() != DAY.replace(hour=8)
    scheduler._schedule_next()
    job = scheduler.scheduler.get_job(MANAGEMENT_JOB_ID)
    assert job is not None
    assert abs(job.trigger.run_date.timestamp() - DAY.replace(hour=4).timestamp()) < 1
    # The dead ``_due_window`` state from the scan scheduler was removed.
    assert not hasattr(scheduler, "_due_window")
    scheduler.shutdown()


def test_management_scheduler_falls_back_to_the_period_provider() -> None:
    from oft.clock import FixedClock

    now = DAY.replace(hour=2, minute=30)
    scheduler = ManagementDaemonScheduler(
        clock=FixedClock(now),
        job=lambda _window: None,
        period_provider=lambda: "8h",
        state_provider=None,
    )

    assert scheduler.next_run() == run_at(next_boundary(now, "8h"), "8h")
    scheduler.shutdown()
