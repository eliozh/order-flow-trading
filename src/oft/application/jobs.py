"""Durable job ledger and the scheduled scan orchestrator.

SQLite is the source of truth (ADR-0007): ``job_runs`` has
``UNIQUE(job_type, window_start)``, every ``RUNNING`` row carries a lease, and
named ``job_locks`` serialize non-periodic work. ``job_locks`` is a SQLite
lease table, so the claim is safe across processes (the daemon is the sole
writer; readers use WAL snapshots). The daemon reconstructs state from these
tables plus ``scan_symbols`` after a restart; no in-memory flag is trusted.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from oft.application.scan_service import ScanError, ScanRequest, ScanService
from oft.application.scheduler import CatchUpPlan, plan_catch_up
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import (
    JobLockRecord,
    JobRunRecord,
    ScanRecord,
    ScanSymbolRecord,
    StorePort,
)

SCAN_JOB_TYPE = "scan"
SCAN_LOCK_NAME = "scan"
DEFAULT_LEASE_SECONDS = 900
HEARTBEAT_SECONDS = 300
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"
MISSED = "MISSED"
SKIPPED = "SKIPPED"
_TERMINAL_SCAN_STAGES = frozenset({"DONE", "FAILED"})


@dataclass(frozen=True)
class JobClaim:
    """Outcome of one atomic claim attempt."""

    claimed: bool
    run: JobRunRecord | None = None
    reclaimed: bool = False
    reason: str = ""

    @property
    def run_id(self) -> str | None:
        return self.run.run_id if self.run is not None else None

    @property
    def running_scan_id(self) -> str | None:
        return self.run.run_id if self.run is not None else None


@dataclass(frozen=True)
class RestartState:
    """Reconstructed scheduler state for one job type after a restart."""

    running: tuple[JobRunRecord, ...]
    latest: JobRunRecord | None
    incomplete_scan_symbols: tuple[ScanSymbolRecord, ...]
    resumable_run_id: str | None


@dataclass(frozen=True)
class ScheduledScanOutcome:
    """Result of one scheduled scan window."""

    run: JobRunRecord | None
    claimed: bool
    scans: tuple[ScanRecord, ...] = ()
    failed_symbols: tuple[str, ...] = ()
    reason: str = ""


class ScanContainer(Protocol):
    @property
    def store(self) -> StorePort: ...

    @property
    def clock(self) -> ClockPort: ...

    @property
    def scan_lock(self) -> Any: ...


def _new_run_id(job_type: str, now: datetime) -> str:
    return f"JOB-{job_type.upper()}-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8].upper()}"


def _boundary_window(value: str) -> datetime | None:
    """Parse a periodic window; manual windows carry a non-timestamp suffix."""
    if "#manual-" in value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class JobLedger:
    """Application-level wrapper around the SQLite job tables."""

    def __init__(self, *, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    @property
    def store(self) -> StorePort:
        return self._store

    def _now(self) -> datetime:
        return self._clock.now()

    def claim(
        self,
        job_type: str,
        window_start: str,
        *,
        trigger_source: str = "SCHEDULED",
        owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        run_id: str | None = None,
    ) -> JobClaim:
        """Claim ``(job_type, window_start)`` once; reclaim only a stale lease."""
        now = self._now()
        lease_owner = owner or f"{job_type}-{uuid.uuid4().hex[:8]}"
        record = JobRunRecord(
            run_id=run_id or _new_run_id(job_type, now),
            job_type=job_type,
            window_start=window_start,
            status=RUNNING,
            trigger_source=trigger_source,
            started_at=utc_timestamp(now),
            finished_at=None,
            error_code=None,
            message=None,
            lease_owner=lease_owner,
            lease_expires_at=utc_timestamp(now + timedelta(seconds=lease_seconds)),
            heartbeat_at=utc_timestamp(now),
        )
        claimed, run, reclaimed = self._store.claim_job_run(record)
        if claimed:
            return JobClaim(claimed=True, run=run, reclaimed=reclaimed, reason="claimed")
        reason = "already_running" if run is not None and run.status == RUNNING else "duplicate"
        return JobClaim(claimed=False, run=run, reclaimed=False, reason=reason)

    def finish(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str | None = None,
        message: str | None = None,
    ) -> bool:
        return self._store.finish_job_run(
            run_id,
            status=status,
            finished_at=utc_timestamp(self._now()),
            error_code=error_code,
            message=message,
        )

    def heartbeat(
        self, run_id: str, *, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        now = self._now()
        return self._store.renew_job_lease(
            run_id,
            owner=owner,
            heartbeat_at=utc_timestamp(now),
            lease_expires_at=utc_timestamp(now + timedelta(seconds=lease_seconds)),
        )

    def active_run(self, job_type: str) -> JobRunRecord | None:
        for run in self._store.list_job_runs(job_type):
            if run.status == RUNNING:
                return run
        return None

    def latest(self, job_type: str) -> JobRunRecord | None:
        return self._store.latest_job_run(job_type)

    def record_missed(
        self,
        job_type: str,
        window_start: str,
        *,
        reason: str,
        trigger_source: str = "SCHEDULED",
    ) -> JobRunRecord:
        """Persist a terminal MISSED row; an existing row wins (idempotent)."""
        now = self._now()
        stamp = utc_timestamp(now)
        record = JobRunRecord(
            run_id=_new_run_id(job_type, now),
            job_type=job_type,
            window_start=window_start,
            status=MISSED,
            trigger_source=trigger_source,
            started_at=stamp,
            finished_at=stamp,
            error_code="MISSED",
            message=reason,
            lease_owner="scheduler",
            lease_expires_at=stamp,
            heartbeat_at=stamp,
        )
        claimed, run, _ = self._store.claim_job_run(record)
        return run if run is not None else record

    def catch_up(
        self,
        *,
        period: str,
        now: datetime,
        last_attempted_window_start: datetime | None,
    ) -> CatchUpPlan:
        """Plan and record the explicit catch-up policy for a scan period."""
        completed = tuple(
            parsed
            for parsed in (
                _boundary_window(run.window_start)
                for run in self._store.list_job_runs(SCAN_JOB_TYPE)
                if run.status in (DONE, RUNNING)
            )
            if parsed is not None
        )
        plan = plan_catch_up(
            now=now,
            period=period,
            last_attempted_window_start=last_attempted_window_start,
            completed_window_starts=completed,
        )
        for missed in plan.missed:
            self.record_missed(
                SCAN_JOB_TYPE,
                utc_timestamp(missed.window_start),
                reason=missed.reason,
            )
        return plan

    def acquire_scan_lock(self, owner: str, *, ttl_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        now = self._now()
        return self._store.acquire_job_lock(
            SCAN_LOCK_NAME,
            owner,
            acquired_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + timedelta(seconds=ttl_seconds)),
        )

    def release_scan_lock(self, owner: str) -> bool:
        return self._store.release_job_lock(SCAN_LOCK_NAME, owner)

    def active_scan(self) -> JobLockRecord | None:
        return self._store.get_job_lock(SCAN_LOCK_NAME)

    def claim_scan(
        self,
        *,
        trigger_source: str = "MANUAL",
        owner: str | None = None,
        window_start: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        run_id: str | None = None,
    ) -> JobClaim:
        """Claim the single active scan slot shared by manual and scheduled runs.

        The SQLite-backed lock is the overlap guard; the ``job_runs`` row is the
        auditable run identity used for the 409 response and restart recovery.
        Manual windows carry a unique suffix so a legitimate rerun in the same
        boundary is not mistaken for a duplicate periodic window.
        """
        now = self._now()
        stamp = utc_timestamp(now)
        lease_owner = owner or f"scan-{uuid.uuid4().hex[:8]}"
        # A crashed owner must not block a new manual scan forever: expire
        # stale RUNNING rows before checking the shared slot.
        for run in self._store.list_job_runs(SCAN_JOB_TYPE):
            if run.status == RUNNING and run.lease_expires_at <= stamp:
                self._store.finish_job_run(
                    run.run_id,
                    status=FAILED,
                    finished_at=stamp,
                    error_code="LEASE_EXPIRED",
                    message="stale RUNNING scan lease expired",
                )
        active = self.active_run(SCAN_JOB_TYPE)
        if active is not None and active.lease_expires_at > stamp:
            return JobClaim(claimed=False, run=active, reason="already_running")
        if not self.acquire_scan_lock(lease_owner, ttl_seconds=lease_seconds):
            running = self.active_run(SCAN_JOB_TYPE)
            return JobClaim(claimed=False, run=running, reason="scan_locked")
        window = window_start or f"{stamp}#manual-{lease_owner}"
        claim = self.claim(
            SCAN_JOB_TYPE,
            window,
            trigger_source=trigger_source,
            owner=lease_owner,
            lease_seconds=lease_seconds,
            run_id=run_id,
        )
        if not claim.claimed:
            self.release_scan_lock(lease_owner)
        return claim

    def reconstruct(self, job_type: str = SCAN_JOB_TYPE) -> RestartState:
        """Rebuild scheduler state from SQLite only (no in-memory assumptions)."""
        runs = tuple(self._store.list_job_runs(job_type))
        running = tuple(run for run in runs if run.status == RUNNING)
        incomplete: list[ScanSymbolRecord] = []
        for run in running:
            incomplete.extend(
                item
                for item in self._store.list_scan_symbols(run.run_id)
                if item.stage not in _TERMINAL_SCAN_STAGES
            )
        return RestartState(
            running=running,
            latest=runs[0] if runs else None,
            incomplete_scan_symbols=tuple(incomplete),
            resumable_run_id=running[0].run_id if running else None,
        )


class ScheduledScanRunner:
    """Run one scheduled scan window over all starred symbols."""

    def __init__(
        self,
        *,
        container: ScanContainer,
        ledger: JobLedger | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._container = container
        self._clock = clock or container.clock
        self._ledger = ledger or JobLedger(store=container.store, clock=self._clock)

    def run_window(
        self,
        window_start: datetime,
        *,
        period: str = "4h",
        trigger_source: str = "SCHEDULED",
    ) -> ScheduledScanOutcome:
        claim = self._ledger.claim(
            SCAN_JOB_TYPE,
            utc_timestamp(window_start),
            trigger_source=trigger_source,
        )
        if not claim.claimed or claim.run is None:
            return ScheduledScanOutcome(run=claim.run, claimed=False, reason=claim.reason)
        run = claim.run
        if not self._ledger.acquire_scan_lock(run.lease_owner):
            # Defer, do not burn the window: remove the provisional run so the
            # next scheduler tick can claim the same window again.
            self._container.store.delete_job_run(run.run_id)
            return ScheduledScanOutcome(run=None, claimed=False, reason="scan_locked")
        stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run.run_id, run.lease_owner, stop),
            daemon=True,
        )
        beat.start()
        try:
            symbols = tuple(contract.symbol for contract in self._container.store.list_starred())
            if claim.reclaimed:
                # Resume a reclaimed run from its persisted checkpoints instead
                # of re-running every starred symbol.
                checkpointed = self._container.store.list_scan_symbols(run.run_id)
                if checkpointed:
                    symbols = tuple(
                        item.symbol
                        for item in checkpointed
                        if item.stage not in _TERMINAL_SCAN_STAGES
                    )
            outcome = self._execute(run, symbols, period=period, trigger_source=run.trigger_source)
            self._ledger.finish(run.run_id, DONE if not outcome.failed_symbols else FAILED)
            return outcome
        finally:
            stop.set()
            beat.join(timeout=1)
            self._ledger.release_scan_lock(run.lease_owner)

    def resume(self, run_id: str, *, period: str = "4h") -> ScheduledScanOutcome:
        """Resume a reclaimed run from its ``scan_symbols`` checkpoints."""
        run = self._container.store.get_job_run(run_id)
        if run is None or run.status != RUNNING:
            return ScheduledScanOutcome(run=run, claimed=False, reason="not_running")
        if not self._ledger.acquire_scan_lock(run.lease_owner):
            return ScheduledScanOutcome(run=run, claimed=False, reason="scan_locked")
        stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run.run_id, run.lease_owner, stop),
            daemon=True,
        )
        beat.start()
        try:
            pending = tuple(
                item.symbol
                for item in self._container.store.list_scan_symbols(run_id)
                if item.stage not in _TERMINAL_SCAN_STAGES
            )
            outcome = self._execute(run, pending, period=period, trigger_source=run.trigger_source)
            self._ledger.finish(run.run_id, DONE if not outcome.failed_symbols else FAILED)
            return outcome
        finally:
            stop.set()
            beat.join(timeout=1)
            self._ledger.release_scan_lock(run.lease_owner)

    def _heartbeat_loop(self, run_id: str, owner: str, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            with suppress(Exception):
                self._ledger.heartbeat(run_id, owner=owner)

    def _execute(
        self,
        run: JobRunRecord,
        symbols: tuple[str, ...],
        *,
        period: str,
        trigger_source: str = "SCHEDULED",
    ) -> ScheduledScanOutcome:
        scans: list[ScanRecord] = []
        failed: list[str] = []
        for symbol in symbols:
            started_at = utc_timestamp(self._clock.now())
            self._container.store.upsert_scan_symbol(
                ScanSymbolRecord(
                    scan_id=run.run_id,
                    symbol=symbol,
                    stage="DATA",
                    started_at=started_at,
                    finished_at=None,
                    error_code=None,
                    report_id=None,
                    data_quality=None,
                )
            )
            try:
                result = ScanService(
                    container=self._container,  # type: ignore[arg-type]
                    lock=self._container.scan_lock,
                ).run(
                    ScanRequest(
                        symbol=symbol,
                        period=period,
                        trigger_source=trigger_source,
                    )
                )
            except ScanError as exc:
                failed.append(symbol)
                self._container.store.upsert_scan_symbol(
                    ScanSymbolRecord(
                        scan_id=run.run_id,
                        symbol=symbol,
                        stage="FAILED",
                        started_at=started_at,
                        finished_at=utc_timestamp(self._clock.now()),
                        error_code=exc.code,
                        report_id=None,
                        data_quality=None,
                    )
                )
            else:
                scans.append(result.scan)
                self._container.store.upsert_scan_symbol(
                    ScanSymbolRecord(
                        scan_id=run.run_id,
                        symbol=symbol,
                        stage="DONE",
                        started_at=started_at,
                        finished_at=result.scan.finished_at,
                        error_code=None,
                        report_id=result.report.report_id,
                        data_quality=result.report.data_quality,
                    )
                )
        return ScheduledScanOutcome(
            run=run,
            claimed=True,
            scans=tuple(scans),
            failed_symbols=tuple(failed),
        )


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DONE",
    "FAILED",
    "JobClaim",
    "JobLedger",
    "MISSED",
    "RestartState",
    "RUNNING",
    "SCAN_JOB_TYPE",
    "SCAN_LOCK_NAME",
    "SKIPPED",
    "ScheduledScanOutcome",
    "ScheduledScanRunner",
]
