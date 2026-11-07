"""UTC-anchored scan scheduling: boundaries, jitter, catch-up, period changes.

The durable job ledger in :mod:`oft.application.jobs` is the source of truth;
this module only computes *when* a scan window becomes due. All instants are
UTC. A period change is recorded as pending and takes effect at the next
boundary of the currently active schedule (ADR-0007).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

SCAN_PERIODS = ("4h", "8h")
PERIOD_HOURS: dict[str, int] = {"4h": 4, "8h": 8}
BOUNDARY_HOURS: dict[str, tuple[int, ...]] = {
    "4h": (0, 4, 8, 12, 16, 20),
    "8h": (0, 8, 16),
}
DEFAULT_SCAN_PERIOD = "4h"
DEFAULT_JITTER_SECONDS = 30
MISFIRE_GRACE_SECONDS = 300
_MISSED_REASON = "superseded by the latest missed window"


def _require_aware(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("scheduling instants must be timezone-aware UTC")
    return instant.astimezone(UTC)


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
    if last_attempted_window_start is None:
        return CatchUpPlan(run_window=due, missed=())
    last = align_to_boundary(last_attempted_window_start, period)
    if last >= due:
        return CatchUpPlan(run_window=None, missed=())

    candidates = _boundaries_between(last, due, period)
    if not candidates:
        return CatchUpPlan(run_window=None, missed=())
    latest = candidates[-1]
    completed = {align_to_boundary(item, period) for item in completed_window_starts}
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

    def period_at(self, instant: datetime) -> str:
        resolved = _require_aware(instant)
        if (
            self.pending_period is not None
            and self.effective_from is not None
            and resolved >= self.effective_from
        ):
            return self.pending_period
        return self.period

    def request_period(self, new_period: str, *, now: datetime) -> ScheduleState:
        validate_period(new_period)
        resolved = _require_aware(now)
        active = self.period_at(resolved)
        if new_period == active:
            collapsed = ScheduleState(period=active, jitter_seconds_max=self.jitter_seconds_max)
            return collapsed
        boundary = next_boundary(resolved, active)
        return replace(
            self,
            pending_period=new_period,
            effective_from=boundary,
        )

    def next_run(self, now: datetime) -> datetime:
        """Next jittered UTC fire time, respecting a pending period change."""
        resolved = _require_aware(now)
        active = self.period_at(resolved)
        candidate = next_boundary(resolved, active)
        if (
            self.pending_period is not None
            and self.effective_from is not None
            and candidate < self.effective_from
        ):
            candidate = self.effective_from
        return run_at(
            candidate,
            self.period_at(candidate),
            max_jitter=self.jitter_seconds_max,
        )

    def next_run_payload(self, now: datetime) -> dict[str, str]:
        """UTC-labelled schedule surface shared by the CLI and daemon status."""
        scheduled = self.next_run(now)
        return {
            "timezone": "UTC",
            "period": self.period_at(now),
            "pending_period": self.pending_period or "",
            "next_run_at": utc_timestamp(scheduled),
        }


@dataclass(frozen=True)
class ScheduleConfig:
    """Scan cadence resolved from ``OFT_SCAN_*`` environment variables."""

    period: str = DEFAULT_SCAN_PERIOD
    jitter_seconds: int = DEFAULT_JITTER_SECONDS


def resolve_schedule_settings(
    environ: Mapping[str, str] | None = None,
) -> ScheduleConfig:
    """Resolve the scan cadence; invalid values raise ``ValueError``."""
    source = environ if environ is not None else os.environ
    period = validate_period(source.get("OFT_SCAN_PERIOD", DEFAULT_SCAN_PERIOD).strip())
    raw_jitter = source.get("OFT_SCAN_JITTER_SECONDS", str(DEFAULT_JITTER_SECONDS)).strip()
    try:
        jitter = int(raw_jitter)
    except ValueError as exc:
        raise ValueError(f"OFT_SCAN_JITTER_SECONDS must be an integer, got {raw_jitter!r}") from exc
    if jitter < 0:
        raise ValueError("OFT_SCAN_JITTER_SECONDS must be >= 0")
    return ScheduleConfig(period=period, jitter_seconds=jitter)


class ScanDaemonScheduler:
    """Thin APScheduler wrapper; the ledger still decides whether work runs."""

    def __init__(
        self,
        *,
        clock: ClockPort,
        state: ScheduleState,
        job: Callable[[datetime], Any],
        timezone: str = "UTC",
    ) -> None:
        self._clock = clock
        self._state = state
        self._job = job
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
        self._schedule_next()

    def start(self) -> None:
        self._schedule_next()
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    def _schedule_next(self) -> None:
        fire_at = self._state.next_run(self._clock.now())
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=fire_at, timezone="UTC"),
            id="scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def _fire(self) -> None:
        now = self._clock.now()
        window = align_to_boundary(now, self._state.period_at(now))
        try:
            self._job(window)
        finally:
            self._schedule_next()
