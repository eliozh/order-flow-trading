"""UTC-anchored scan scheduling: boundaries, jitter, catch-up, period changes.

The durable job ledger in :mod:`oft.application.jobs` is the source of truth;
this module only computes *when* a scan window becomes due. All instants are
UTC. A period change is recorded as pending and takes effect at the next
boundary of the currently active schedule (ADR-0007).

It also hosts the independent 15 s trigger interval scheduler
(:class:`TriggerDaemonScheduler`), whose ledger claim is likewise the source of
truth.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from oft.config import (
    DEFAULT_SCAN_JITTER_SECONDS,
    DEFAULT_SCAN_PERIOD,
    SCAN_JITTER_ENV,
    SCAN_PERIOD_ENV,
    Settings,
    validate_scan_jitter_setting,
    validate_scan_period_setting,
)
from oft.domain.settings import SCAN_PERIOD_HOURS, SUPPORTED_SCAN_PERIODS
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

SCAN_PERIODS = SUPPORTED_SCAN_PERIODS
# Derived from the one domain mapping so a domain cadence change cannot leave
# the scheduler's hours/boundaries behind (M10).
PERIOD_HOURS: dict[str, int] = dict(SCAN_PERIOD_HOURS)
BOUNDARY_HOURS: dict[str, tuple[int, ...]] = {
    period: tuple(range(0, 24, hours)) for period, hours in SCAN_PERIOD_HOURS.items()
}
DEFAULT_JITTER_SECONDS = DEFAULT_SCAN_JITTER_SECONDS
MISFIRE_GRACE_SECONDS = 300
# A fresh heartbeat proves the daemon's interval scheduler is alive. The daemon
# writes it on startup and on every settings-sync tick (~15 s), so a minute of
# silence means the scheduler thread died or an API-only process is serving.
SCHEDULER_HEARTBEAT_STALE_SECONDS = 60
SCHEDULER_HEARTBEAT_FILENAME = "scheduler_heartbeat.json"
# Management is armed after the scan's jittered fire time so a position opened by
# that scan is usually managed in the same window. A scan that runs longer than
# the stagger defers the new position to the next boundary.
MANAGEMENT_STAGGER_SECONDS = 60
_MISSED_REASON = "superseded by the latest missed window"


def _require_aware(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("scheduling instants must be timezone-aware UTC")
    return instant.astimezone(UTC)


def scheduler_heartbeat_path(settings: Settings) -> Path:
    """Where the daemon records scheduler liveness (never read by schedulers)."""
    return settings.data_dir / "run" / SCHEDULER_HEARTBEAT_FILENAME


def write_scheduler_heartbeat(settings: Settings, *, now: datetime) -> None:
    """Atomically record one scheduler tick; a write failure never raises.

    The heartbeat is a readiness input, not a trading input: a full disk or a
    permission change must degrade ``/ready`` rather than kill the scheduler
    thread that owns this tick.
    """
    path = scheduler_heartbeat_path(settings)
    # The temp name is unique per process *and* thread: the startup write and
    # a concurrent settings-sync tick must not interleave write/replace/unlink
    # and silently lose one heartbeat.
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps({"updated_at": utc_timestamp(now)}), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def read_scheduler_heartbeat(settings: Settings) -> str | None:
    """The recorded ISO timestamp, or ``None`` when absent/corrupt/unreadable."""
    try:
        payload = json.loads(scheduler_heartbeat_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("updated_at")
    return value if isinstance(value, str) else None


def validate_period(period: str) -> str:
    """Return ``period`` when it is a supported scan cadence."""
    if period not in PERIOD_HOURS:
        raise ValueError(f"unsupported scan period {period!r}; expected one of {SCAN_PERIODS}")
    return period


def boundary_hours(period: str) -> tuple[int, ...]:
    return BOUNDARY_HOURS[validate_period(period)]


def is_boundary(instant: datetime, period: str) -> bool:
    resolved = _require_aware(instant)
    return (
        resolved.hour in boundary_hours(period)
        and resolved.minute == 0
        and resolved.second == 0
        and resolved.microsecond == 0
    )


def align_to_boundary(instant: datetime, period: str) -> datetime:
    """Floor an instant to the containing UTC boundary."""
    resolved = _require_aware(instant)
    hours = boundary_hours(period)
    hour = max(candidate for candidate in hours if candidate <= resolved.hour)
    return resolved.replace(hour=hour, minute=0, second=0, microsecond=0)


def next_boundary(instant: datetime, period: str) -> datetime:
    """Return the first boundary strictly after ``instant``."""
    resolved = _require_aware(instant)
    aligned = align_to_boundary(resolved, period)
    if aligned > resolved:
        return aligned
    return aligned + timedelta(hours=PERIOD_HOURS[validate_period(period)])


def window_for(instant: datetime, period: str) -> tuple[datetime, datetime]:
    """Return the half-open ``[start, end)`` window containing ``instant``."""
    start = align_to_boundary(instant, period)
    return start, start + timedelta(hours=PERIOD_HOURS[validate_period(period)])


def jitter_seconds(
    period: str, window_start: datetime, *, max_jitter: int = DEFAULT_JITTER_SECONDS
) -> int:
    """Deterministic 0..max_jitter-1 offset for one window (no randomness)."""
    if max_jitter <= 0:
        return 0
    key = f"{validate_period(period)}|{utc_timestamp(window_start)}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max_jitter


def run_at(
    window_start: datetime, period: str, *, max_jitter: int = DEFAULT_JITTER_SECONDS
) -> datetime:
    """Jittered wall-clock fire time for a boundary window."""
    return _require_aware(window_start) + timedelta(
        seconds=jitter_seconds(period, window_start, max_jitter=max_jitter)
    )


def latest_due_window(now: datetime, period: str) -> datetime:
    """Latest boundary at or before ``now``."""
    return align_to_boundary(now, period)


@dataclass(frozen=True)
class MissedWindow:
    """An older downtime window that is recorded but never replayed."""

    window_start: datetime
    reason: str


@dataclass(frozen=True)
class CatchUpPlan:
    """The one runnable missed window plus the older windows to mark MISSED."""

    run_window: datetime | None
    missed: tuple[MissedWindow, ...]


def _boundaries_between(last: datetime, due: datetime, period: str) -> tuple[datetime, ...]:
    step = timedelta(hours=PERIOD_HOURS[validate_period(period)])
    windows: list[datetime] = []
    cursor = last + step
    while cursor <= due:
        windows.append(cursor)
        cursor += step
    return tuple(windows)


def plan_catch_up(
    *,
    now: datetime,
    period: str,
    last_attempted_window_start: datetime | None,
    completed_window_starts: tuple[datetime, ...] = (),
) -> CatchUpPlan:
    """Choose at most one window to run after downtime.

    Only the most recent missed window is eligible. Every older missed window
    is returned with a reason so the ledger can record ``MISSED``; it is never
    replayed (ADR-0007 catch-up policy).
    """
    due = latest_due_window(_require_aware(now), period)
    completed = {align_to_boundary(item, period) for item in completed_window_starts}
    if last_attempted_window_start is None:
        return CatchUpPlan(run_window=None if due in completed else due, missed=())
    last = align_to_boundary(last_attempted_window_start, period)
    if last >= due:
        return CatchUpPlan(run_window=None, missed=())

    candidates = _boundaries_between(last, due, period)
    if not candidates:
        return CatchUpPlan(run_window=None, missed=())
    latest = candidates[-1]
    missed = tuple(
        MissedWindow(window_start=window, reason=_MISSED_REASON) for window in candidates[:-1]
    )
    return CatchUpPlan(
        run_window=None if latest in completed else latest,
        missed=missed,
    )


@dataclass(frozen=True)
class ScheduleState:
    """Active period plus an optional pending change that applies at a boundary."""

    period: str = DEFAULT_SCAN_PERIOD
    pending_period: str | None = None
    effective_from: datetime | None = None
    jitter_seconds_max: int = DEFAULT_JITTER_SECONDS

    def __post_init__(self) -> None:
        validate_period(self.period)
        if self.pending_period is not None:
            validate_period(self.pending_period)
        if self.effective_from is not None:
            _require_aware(self.effective_from)
        if self.jitter_seconds_max < 0:
            raise ValueError("jitter_seconds_max must be >= 0")
        periods = [self.period]
        if self.pending_period is not None:
            periods.append(self.pending_period)
        if self.jitter_seconds_max >= min(PERIOD_HOURS[item] for item in periods) * 3600:
            raise ValueError("jitter_seconds_max must be less than the scan period")

    def period_at(self, instant: datetime) -> str:
        resolved = _require_aware(instant)
        if (
            self.pending_period is not None
            and self.effective_from is not None
            and resolved >= self.effective_from
        ):
            return self.pending_period
        return self.period

    def promote(self, now: datetime) -> ScheduleState:
        """Apply a pending change whose boundary has passed; else return ``self``.

        A ``frozen`` state never mutates, so promotion returns a *new* state
        with the pending period as the active one and no pending change. This is
        the normalisation every read path (``next_run``, status payloads, the
        periodic sync) uses so a change that already applied is never reported
        as still pending.
        """
        resolved = _require_aware(now)
        if (
            self.pending_period is not None
            and self.effective_from is not None
            and resolved >= self.effective_from
        ):
            return ScheduleState(
                period=self.pending_period,
                pending_period=None,
                effective_from=None,
                jitter_seconds_max=self.jitter_seconds_max,
            )
        return self

    def request_period(self, new_period: str, *, now: datetime) -> ScheduleState:
        validate_period(new_period)
        resolved = _require_aware(now)
        state = self.promote(resolved)
        active = state.period_at(resolved)
        if new_period == active:
            return ScheduleState(period=active, jitter_seconds_max=state.jitter_seconds_max)
        boundary = next_boundary(resolved, active)
        return replace(
            state,
            pending_period=new_period,
            effective_from=boundary,
        )

    def apply_persisted(
        self, persisted: str | None, *, now: datetime
    ) -> tuple[ScheduleState, bool]:
        """Overlay one persisted period on the schedule without rewriting a window.

        Promotion happens first, even when no persisted value is present: once a
        pending change's boundary has passed it is the active period, and a later
        revert must be requested as a *new* pending change rather than treated as
        an immediate no-op against a stale ``pending_period``.

        Returns ``(state, changed)``. ``None`` and an already-active or already
        pending value are no-ops. A persisted value equal to the active period
        collapses a stale future pending change (an operator revert); any other
        value is requested as a pending change that applies at the next boundary
        of the active schedule.
        """
        resolved = _require_aware(now)
        promoted = self.promote(resolved)
        if persisted is None:
            return promoted, promoted is not self
        validate_period(persisted)
        active = promoted.period_at(resolved)
        if persisted == active:
            if promoted.pending_period is None or promoted.pending_period == active:
                return promoted, promoted is not self
            collapsed = ScheduleState(period=active, jitter_seconds_max=promoted.jitter_seconds_max)
            return collapsed, True
        if persisted == promoted.pending_period:
            return promoted, promoted is not self
        return promoted.request_period(persisted, now=resolved), True

    def next_run(self, now: datetime) -> datetime:
        """Next jittered UTC fire time, respecting a pending period change."""
        resolved = _require_aware(now)
        state = self.promote(resolved)
        active = state.period_at(resolved)
        candidate = next_boundary(resolved, active)
        if (
            state.pending_period is not None
            and state.effective_from is not None
            and candidate < state.effective_from
        ):
            candidate = state.effective_from
        return run_at(
            candidate,
            state.period_at(candidate),
            max_jitter=state.jitter_seconds_max,
        )

    def next_run_payload(self, now: datetime) -> dict[str, str]:
        """UTC-labelled schedule surface shared by the CLI and daemon status."""
        resolved = _require_aware(now)
        state = self.promote(resolved)
        scheduled = state.next_run(resolved)
        return {
            "timezone": "UTC",
            "period": state.period_at(resolved),
            "pending_period": state.pending_period or "",
            "next_run_at": utc_timestamp(scheduled),
        }


@dataclass(frozen=True)
class ScheduleConfig:
    """Scan cadence resolved from the configured ``OFT_SCAN_*`` values."""

    period: str = DEFAULT_SCAN_PERIOD
    jitter_seconds: int = DEFAULT_JITTER_SECONDS


def _cadence_mapping(settings: Settings) -> dict[str, str]:
    """The cadence keys one loaded ``Settings`` resolved (``.env`` included)."""
    return {
        SCAN_PERIOD_ENV: settings.scan_period,
        SCAN_JITTER_ENV: str(settings.scan_jitter_seconds),
    }


def _cadence_source(
    environ: Mapping[str, str] | None, settings: Settings | None
) -> Mapping[str, str]:
    """Pick the cadence source: explicit mapping, explicit settings, else loaded.

    An explicit ``environ`` mapping is authoritative and never consults the
    environment or ``.env``. An explicit ``settings`` uses the values that
    object already loaded. With neither, a fresh :class:`Settings` is loaded, so
    ``OFT_SCAN_*`` values from ``.env`` reach the resolvers exactly as they reach
    the rest of the application.
    """
    if environ is not None:
        return environ
    if settings is not None:
        return _cadence_mapping(settings)
    return _cadence_mapping(Settings())


def resolve_scan_jitter(
    environ: Mapping[str, str] | None = None,
    *,
    settings: Settings | None = None,
) -> int:
    """Parse ``OFT_SCAN_JITTER_SECONDS`` without requiring a valid env period.

    Diagnostics resolve the persisted period first (M3); this helper lets the
    overlay build a jittered default even when ``OFT_SCAN_PERIOD`` itself would
    be rejected, so only the jitter's own errors surface. A blank value means
    the default, mirroring ``Settings``; a non-integer or negative value raises
    ``ValueError`` (``CONFIG_INVALID``). The bounds rule is shared with
    ``oft.config`` (one source of truth).

    With no mapping the value comes from the loaded :class:`Settings`, so a
    ``.env`` value is honored; an injected ``environ`` or ``settings`` object is
    authoritative.
    """
    source = _cadence_source(environ, settings)
    raw_jitter = source.get(SCAN_JITTER_ENV, str(DEFAULT_SCAN_JITTER_SECONDS)).strip()
    if not raw_jitter:
        return DEFAULT_JITTER_SECONDS
    try:
        jitter = int(raw_jitter)
    except ValueError as exc:
        raise ValueError(f"{SCAN_JITTER_ENV} must be an integer, got {raw_jitter!r}") from exc
    return validate_scan_jitter_setting(jitter)


def resolve_schedule_settings(
    environ: Mapping[str, str] | None = None,
    *,
    settings: Settings | None = None,
) -> ScheduleConfig:
    """Resolve the scan cadence; invalid values raise ``ValueError``.

    The rule (supported period plus a jitter shorter than that period) is
    shared with ``oft.config``. With no mapping the cadence is read from the
    loaded :class:`Settings`, so a ``.env`` value takes effect; an injected
    mapping or ``settings`` object is authoritative and never falls back to the
    process environment or ``.env``.
    """
    source = _cadence_source(environ, settings)
    period = validate_scan_period_setting(source.get(SCAN_PERIOD_ENV, DEFAULT_SCAN_PERIOD))
    jitter = resolve_scan_jitter(source)
    validate_scan_jitter_setting(jitter, period)
    return ScheduleConfig(period=period, jitter_seconds=jitter)


def schedule_state_path(settings: Settings) -> Path:
    """Where the daemon persists the active scan cadence."""
    return settings.data_dir / "run" / "schedule.json"


def resolve_schedule_state(settings: Settings) -> ScheduleState:
    """The persisted state, falling back to the settings' ``OFT_SCAN_*`` values.

    The fallback comes from the supplied ``settings`` object (which already
    loaded ``.env``), so an unpersisted cadence set only in ``.env`` is honored.
    """
    config = resolve_schedule_settings(settings=settings)
    default = ScheduleState(period=config.period, jitter_seconds_max=config.jitter_seconds)
    return load_schedule_state(schedule_state_path(settings), default=default)


def load_schedule_state(path: Any, *, default: ScheduleState) -> ScheduleState:
    """Load a persisted period/pending change, falling back to the default."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default
    try:
        effective = payload.get("effective_from")
        return ScheduleState(
            period=str(payload.get("period") or default.period),
            pending_period=payload.get("pending_period") or None,
            effective_from=(
                datetime.fromisoformat(str(effective).replace("Z", "+00:00")) if effective else None
            ),
            jitter_seconds_max=int(payload.get("jitter_seconds_max", default.jitter_seconds_max)),
        )
    except (ValueError, TypeError):
        return default


def save_schedule_state(path: Any, state: ScheduleState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "period": state.period,
                "pending_period": state.pending_period,
                "effective_from": (
                    utc_timestamp(state.effective_from)
                    if state.effective_from is not None
                    else None
                ),
                "jitter_seconds_max": state.jitter_seconds_max,
            }
        ),
        encoding="utf-8",
    )


class ScanDaemonScheduler:
    """Thin APScheduler wrapper; the ledger still decides whether work runs."""

    def __init__(
        self,
        *,
        clock: ClockPort,
        state: ScheduleState,
        job: Callable[[datetime], Any],
        timezone: str = "UTC",
        state_path: Any | None = None,
        on_missed: Callable[[datetime], None] | None = None,
    ) -> None:
        self._clock = clock
        self._state = state
        self._job = job
        self._state_path = state_path
        self._on_missed = on_missed
        self._due_window: datetime | None = None
        self._scheduler = BackgroundScheduler(timezone=timezone)

    @property
    def state(self) -> ScheduleState:
        return self._state

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    def next_run(self, now: datetime | None = None) -> datetime:
        return self._state.next_run(now or self._clock.now())

    def request_period(self, new_period: str, *, now: datetime | None = None) -> None:
        self._state = self._state.request_period(new_period, now=now or self._clock.now())
        if self._state_path is not None:
            save_schedule_state(self._state_path, self._state)
        self._schedule_next()

    def refresh_period(self, new_period: str | None, *, now: datetime | None = None) -> bool:
        """Apply a persisted period (or just promote) only when it changes state.

        Returns ``True`` when a pending change was requested, a stale pending
        change was collapsed, or a due pending change was promoted. Repeating
        the same value (including while the change is pending) is a no-op, so a
        periodic sync cannot keep shifting ``effective_from`` or rewrite the
        current window.
        """
        resolved = now or self._clock.now()
        updated, changed = self._state.apply_persisted(new_period, now=resolved)
        if not changed:
            return False
        self._state = updated
        if self._state_path is not None:
            save_schedule_state(self._state_path, self._state)
        self._schedule_next()
        return True

    def start(self) -> None:
        self._schedule_next()
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    def _schedule_next(self) -> None:
        fire_at = self._state.next_run(self._clock.now())
        period = self._state.period_at(fire_at)
        self._due_window = align_to_boundary(fire_at, period)
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=fire_at, timezone="UTC"),
            id="scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )

    def _fire(self) -> None:
        now = self._clock.now()
        current = align_to_boundary(now, self._state.period_at(now))
        window = self._due_window or current
        if window < current:
            step = timedelta(hours=PERIOD_HOURS[self._state.period_at(window)])
            skipped = window
            while skipped < current:
                if self._on_missed is not None:
                    self._on_missed(skipped)
                skipped += step
            window = current
        try:
            self._job(window)
        finally:
            self._schedule_next()


TRIGGER_JOB_ID = "trigger_evaluate"


class SettingsPeriodSync:
    """Apply a persisted scan period to the live scheduler without a restart.

    The daemon runs :meth:`sync` on a short interval. ``period_provider`` returns
    the persisted value (``None`` when no operator row exists), and the
    scheduler only requests a change when it differs from the active or pending
    period, so the current window is never rewritten.
    """

    def __init__(
        self,
        *,
        scheduler: ScanDaemonScheduler,
        period_provider: Callable[[], str | None],
    ) -> None:
        self._scheduler = scheduler
        self._period_provider = period_provider

    def sync(self) -> bool:
        # ``None`` still promotes a due pending change, so the sync must always
        # reach the scheduler; only the scheduler knows whether state changed.
        return self._scheduler.refresh_period(self._period_provider())


class _IntervalDaemonScheduler:
    """A single-instance interval scheduler whose durable ledger is the truth.

    ``max_instances=1`` and ``coalesce=True`` skip an overlapping tick, and a
    small misfire grace avoids replaying missed windows. Only the current
    interval window is ever claimed by the runner, so a restart or a late tick
    never replays an older window.
    """

    def __init__(
        self,
        *,
        job_id: str,
        interval_seconds: int,
        job: Callable[[], Any],
        timezone: str = "UTC",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._interval_seconds = interval_seconds
        self._scheduler = BackgroundScheduler(timezone=timezone)
        self._scheduler.add_job(
            job,
            trigger=IntervalTrigger(seconds=interval_seconds, timezone=timezone),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(1, min(MISFIRE_GRACE_SECONDS, interval_seconds)),
        )

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)


class TriggerDaemonScheduler(_IntervalDaemonScheduler):
    """The 15 s interval scheduler; the durable ledger decides whether work runs."""

    def __init__(
        self,
        *,
        interval_seconds: int,
        job: Callable[[], Any],
        timezone: str = "UTC",
    ) -> None:
        super().__init__(
            job_id=TRIGGER_JOB_ID,
            interval_seconds=interval_seconds,
            job=job,
            timezone=timezone,
        )


POSITION_MONITOR_JOB_ID = "position_monitor"
MANAGEMENT_JOB_ID = "position_manage"
CONFIG_SYNC_JOB_ID = "config_sync"
CONFIG_SYNC_INTERVAL_SECONDS = 15


class SettingsSyncDaemonScheduler(_IntervalDaemonScheduler):
    """The ~15 s interval that applies a saved ``scan.period`` without a restart."""

    def __init__(
        self,
        *,
        job: Callable[[], Any],
        interval_seconds: int = CONFIG_SYNC_INTERVAL_SECONDS,
        timezone: str = "UTC",
    ) -> None:
        super().__init__(
            job_id=CONFIG_SYNC_JOB_ID,
            interval_seconds=interval_seconds,
            job=job,
            timezone=timezone,
        )


class PositionMonitorDaemonScheduler(_IntervalDaemonScheduler):
    """The position-monitor interval scheduler (code-only protective tick)."""

    def __init__(
        self,
        *,
        interval_seconds: int,
        job: Callable[[], Any],
        timezone: str = "UTC",
    ) -> None:
        super().__init__(
            job_id=POSITION_MONITOR_JOB_ID,
            interval_seconds=interval_seconds,
            job=job,
            timezone=timezone,
        )


class ManagementDaemonScheduler:
    """The scan-aligned management cadence, staggered after the scan window.

    ADR-0007 arms ``position_manage`` at each period boundary, staggered after
    the scan, and requires one run for the open positions after a restart. This
    scheduler adds :data:`MANAGEMENT_STAGGER_SECONDS` to the scan's jittered
    fire time so a position entered by that scan is usually manageable in the
    same window (a scan longer than the stagger defers it to the next boundary).
    It never replays a missed *periodic* window: a late fire runs only
    the window containing the current clock instant. The restart catch-up is a
    separate, explicit daemon step (see ``_catch_up_management_on_start``).

    A ``DateTrigger`` is re-armed for the next scan boundary after every fire.
    When a live :class:`ScheduleState` is supplied through ``state_provider``,
    its ``next_run``/``period_at`` semantics are used, so a pending period
    change re-arms at its transition boundary instead of being skipped until
    the old period's next boundary. Without a state the ``period_provider``
    fallback is used.
    """

    def __init__(
        self,
        *,
        clock: ClockPort,
        job: Callable[[datetime], Any],
        period_provider: Callable[[], str],
        state_provider: Callable[[], ScheduleState] | None = None,
        timezone: str = "UTC",
    ) -> None:
        self._clock = clock
        self._job = job
        self._period_provider = period_provider
        self._state_provider = state_provider
        self._scheduler = BackgroundScheduler(timezone=timezone)

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    def _schedule_state(self) -> ScheduleState | None:
        return None if self._state_provider is None else self._state_provider()

    def next_run(self, now: datetime | None = None) -> datetime:
        resolved = now or self._clock.now()
        state = self._schedule_state()
        if state is None:
            period = validate_period(self._period_provider())
            base = run_at(next_boundary(resolved, period), period)
            return base + timedelta(seconds=MANAGEMENT_STAGGER_SECONDS)
        scheduled = state.next_run(resolved)
        effective = state.effective_from
        if (
            state.pending_period is not None
            and effective is not None
            and resolved < effective < scheduled
        ):
            # The pending period begins at a boundary of the new period; run
            # that window instead of waiting for the old period's next one.
            scheduled = run_at(
                effective,
                state.pending_period,
                max_jitter=state.jitter_seconds_max,
            )
        return scheduled + timedelta(seconds=MANAGEMENT_STAGGER_SECONDS)

    def start(self) -> None:
        self._schedule_next()
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    def _schedule_next(self) -> None:
        fire_at = self.next_run()
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=fire_at, timezone="UTC"),
            id=MANAGEMENT_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )

    def _fire(self) -> None:
        now = self._clock.now()
        state = self._schedule_state()
        period = validate_period(self._period_provider()) if state is None else state.period_at(now)
        # No catch-up: only the window containing the current instant runs.
        window = align_to_boundary(now, period)
        try:
            self._job(window)
        finally:
            self._schedule_next()
