"""Durable job ledger and the scheduled scan orchestrator.

SQLite is the source of truth (ADR-0007): ``job_runs`` has
``UNIQUE(job_type, window_start)``, every ``RUNNING`` row carries a lease, and
named ``job_locks`` serialize non-periodic work. ``job_locks`` is a SQLite
lease table, so the claim is safe across processes (the daemon is the sole
writer; readers use WAL snapshots). The daemon reconstructs state from these
tables plus ``scan_symbols`` after a restart; no in-memory flag is trusted.

Deliberate size exception: this module stays above the project's 800-line soft
ceiling because the durable ledger, the three scheduled runners, and their
closed outcome vocabularies are one cohesive job-orchestration unit. Every
runner shares the same claim/finish policy, so splitting them would duplicate
the lease and status semantics. The largest separable concerns (scan service,
position service, management service) already live in their own modules.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

import structlog

from oft.application.management_service import (
    ManagementCondition,
    ManagementRejected,
    ManagementResult,
    ManagementRiskContext,
    ManagementService,
)
from oft.application.position_service import (
    PositionCloseError,
    PositionMonitorResult,
    PositionService,
)
from oft.application.scan_service import (
    ScanAlreadyRunning,
    ScanError,
    ScanRequest,
    ScanService,
)
from oft.application.scheduler import (
    PERIOD_HOURS,
    CatchUpPlan,
    align_to_boundary,
    plan_catch_up,
    validate_period,
)
from oft.application.trigger_service import TriggerBatchOutcome, TriggerService
from oft.domain.risk import ManagementStatus
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
TRIGGER_JOB_TYPE = "trigger_evaluate"
TRIGGER_JOB_SOURCE = "trigger"
POSITION_MONITOR_JOB_TYPE = "position_monitor"
POSITION_MANAGE_JOB_TYPE = "position_manage"
# ``position_manage`` writes one row per position per window. The ledger key is
# ``"{window_start_iso}#{position_id}"`` (see ``ManagementJobRunner._run_position``);
# the part before ``#`` is the boundary window shared by every row of the window.
POSITION_MANAGE_SOURCE = "position_manage"
DEFAULT_TRIGGER_INTERVAL_SECONDS = 15
DEFAULT_POSITION_MONITOR_INTERVAL_SECONDS = 15
# The code-only protective-exit reason recorded in the position-monitor job
# outcome. The position monitor result's own contract remains ``STOP_TOUCH``.
PROTECTIVE_STOP = "PROTECTIVE_STOP"
DEFAULT_LEASE_SECONDS = 900
HEARTBEAT_SECONDS = 300
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"
PARTIAL = "PARTIAL"
MISSED = "MISSED"
SKIPPED = "SKIPPED"


class PositionTickStatus(StrEnum):
    """The closed outcome vocabulary for one position inside a monitor tick."""

    CLOSED = "CLOSED"
    NO_ACTION = "NO_ACTION"
    ERROR = "ERROR"


class PositionMonitorStatus(StrEnum):
    """The closed aggregate status of one ``position_monitor`` tick."""

    DONE = "DONE"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ManagementPositionStatus(StrEnum):
    """The closed per-position outcome of one management window.

    ``REFUSED`` is a typed decision refusal (the kill switch, a gate block, a
    provider rejection); ``FAILED`` is a hard failure such as a crash or a
    transient provider outage that must be retried. They are deliberately
    distinct so a refused position is not reported as an infrastructure fault
    (and vice versa).
    """

    DONE = "DONE"
    REFUSED = "REFUSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ManagementWindowStatus(StrEnum):
    """The closed aggregate status of one management window."""

    DONE = "DONE"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


# Transient LLM/infrastructure codes finish FAILED so the next window retries
# honestly instead of recording a terminal refusal (M15).
_TRANSIENT_MANAGEMENT_CODES = frozenset({"LLM_TIMEOUT", "LLM_RATE_LIMITED", "UPSTREAM_UNAVAILABLE"})
# Routine position-state skips are expected, not failures. Every other
# ``PositionCloseError`` is classified explicitly: gate/broker refusals are
# REFUSED, and everything else (DATA_UNAVAILABLE, CONFLICT, ...) is a hard
# failure so a broken read is not hidden as a skip (silent-failure N1).
_ROUTINE_POSITION_SKIP_CODES = frozenset({"DATA_STALE", "POSITION_STATE_INVALID"})
_POSITION_REFUSAL_CODES = frozenset(
    {"RISK_BLOCKED", "ORDER_REJECTED", "RISK_VERDICT_MISSING", "RISK_VERDICT_INVALID"}
)
_LOGGER = structlog.get_logger(__name__)
_TERMINAL_SCAN_STAGES = frozenset({"DONE", "DEGRADED", "FAILED"})
_FAILED_STAGE_BY_CODE = {
    "DATA_UNAVAILABLE": "DATA",
    "METRICS_INCOMPLETE": "METRICS",
}


def _failure_stage(exc: BaseException, code: str) -> str:
    """The stage that failed, from the scan service or the error code."""
    stage = getattr(exc, "failed_stage", None)
    if stage:
        return str(stage)
    return _FAILED_STAGE_BY_CODE.get(code) or "REPORT"


@dataclass(frozen=True)
class JobClaim:
    """Outcome of one atomic claim attempt."""

    claimed: bool
    run: JobRunRecord | None = None
    reclaimed: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        # A claimed attempt always owns the run it will finish. A refused
        # attempt may still carry the existing run so callers can report it.
        if self.claimed and self.run is None:
            raise ValueError("a claimed job claim requires its run row")

    @property
    def run_id(self) -> str | None:
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
    degraded_symbols: tuple[str, ...] = ()
    reason: str = ""
    symbols_total: int = 0
    total_tokens: int = 0
    total_cost_usd: str = "0"
    cost_unpriced: bool = False


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


def _pending_symbols(
    run: JobRunRecord,
    rows: tuple[ScanSymbolRecord, ...],
    starred: tuple[str, ...],
) -> tuple[str, ...]:
    """Resume scope: a single-symbol run stays on its symbol; a batch uses starring."""
    terminal = {item.symbol for item in rows if item.stage in _TERMINAL_SCAN_STAGES}
    if run.symbol is not None:
        return () if run.symbol in terminal else (run.symbol,)
    return tuple(symbol for symbol in starred if symbol not in terminal)


def _outcome_status(outcome: ScheduledScanOutcome) -> str:
    """Persist ``PARTIAL`` when only some symbols succeeded, ``FAILED`` otherwise."""
    if not outcome.failed_symbols:
        return DONE
    return PARTIAL if outcome.scans else FAILED


def _rows_status(rows: tuple[ScanSymbolRecord, ...]) -> str:
    """Derive a run status from terminal checkpoint rows only.

    Matches ``_batch_status`` semantics: ``DONE`` when every terminal row
    finished cleanly, ``FAILED`` when nothing succeeded, and ``PARTIAL`` for
    mixed or degraded outcomes.
    """
    terminal = tuple(row for row in rows if row.stage in _TERMINAL_SCAN_STAGES)
    if not terminal:
        return FAILED
    if all(row.stage == DONE for row in terminal):
        return DONE
    if all(row.stage == FAILED for row in terminal):
        return FAILED
    return PARTIAL


def _merged_status(
    outcome: ScheduledScanOutcome,
    rows: tuple[ScanSymbolRecord, ...],
) -> str:
    """Merge this pass's outcome with every terminal checkpoint row for the run.

    ``run_window`` and ``resume`` only execute pending symbols, so the in-pass
    outcome alone hides a prior owner's terminal ``FAILED`` or ``DEGRADED``
    checkpoints. With no terminal rows there is nothing to merge; the pass's
    own outcome is authoritative.
    """
    if not any(row.stage in _TERMINAL_SCAN_STAGES for row in rows):
        return _outcome_status(outcome)
    outcome_status = _outcome_status(outcome)
    rows_status = _rows_status(rows)
    if outcome_status == DONE and rows_status == DONE:
        return DONE
    if outcome_status == FAILED and rows_status == FAILED:
        return FAILED
    return PARTIAL


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
        source: str = "backfill",
        metrics_only: bool = False,
        provider: str | None = None,
        symbol: str | None = None,
        period: str | None = None,
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
            source=source,
            metrics_only=metrics_only,
            provider=provider,
            symbol=symbol,
            period=period,
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
        owner: str | None = None,
    ) -> bool:
        return self._store.finish_job_run(
            run_id,
            status=status,
            finished_at=utc_timestamp(self._now()),
            error_code=error_code,
            message=message,
            lease_owner=owner,
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
        source: str = "backfill",
        metrics_only: bool = False,
        provider: str | None = None,
        symbol: str | None = None,
        period: str | None = None,
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
        # A crashed owner must not block a new manual scan forever. This is a
        # single conditional UPDATE, so a concurrent heartbeat cannot be
        # clobbered between a read and a write.
        self._store.fail_stale_job_runs(job_type=SCAN_JOB_TYPE, now=stamp)
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
            source=source,
            metrics_only=metrics_only,
            provider=provider,
            symbol=symbol,
            period=period,
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
            period=period,
        )
        if not claim.claimed or claim.run is None:
            return ScheduledScanOutcome(run=claim.run, claimed=False, reason=claim.reason)
        run = claim.run
        if not self._ledger.acquire_scan_lock(run.lease_owner):
            if claim.reclaimed:
                # Preserve another owner's run row and checkpoints for audit;
                # mark it failed so it is visible instead of silently dropped.
                self._ledger.finish(
                    run.run_id,
                    FAILED,
                    error_code="SCAN_ALREADY_RUNNING",
                    message="reclaimed run could not acquire the scan lock",
                    owner=run.lease_owner,
                )
            else:
                # Fresh provisional run: remove it so the next tick can claim
                # the same window again.
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
                # Resume a reclaimed run from its persisted checkpoints: the
                # current starred symbols minus terminal checkpoint rows. A
                # single-symbol run resumes only its persisted symbol, never the
                # whole starred universe.
                rows = self._container.store.list_scan_symbols(run.run_id)
                symbols = _pending_symbols(run, rows, symbols)
            outcome = self._execute(
                run,
                symbols,
                period=run.period or period,
                trigger_source=run.trigger_source,
                source=run.source,
                metrics_only=run.metrics_only,
                provider=run.provider,
            )
            self._ledger.finish(
                run.run_id,
                _merged_status(outcome, self._container.store.list_scan_symbols(run.run_id)),
                owner=run.lease_owner,
            )
            return outcome
        except BaseException as exc:
            self._ledger.finish(
                run.run_id,
                FAILED,
                error_code=str(getattr(exc, "code", "JOB_FAILED")),
                message=str(exc),
                owner=run.lease_owner,
            )
            raise
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
        rechecked = self._container.store.get_job_run(run_id)
        if rechecked is None or rechecked.status != RUNNING:
            self._ledger.release_scan_lock(run.lease_owner)
            return ScheduledScanOutcome(run=rechecked, claimed=False, reason="not_running")
        run = rechecked
        stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run.run_id, run.lease_owner, stop),
            daemon=True,
        )
        beat.start()
        try:
            rows = self._container.store.list_scan_symbols(run_id)
            starred = tuple(contract.symbol for contract in self._container.store.list_starred())
            if run.symbol is not None:
                # A single-symbol run resumes only the symbol it was asked for,
                # even if the crash happened before its checkpoint row landed.
                pending = _pending_symbols(run, rows, starred)
                if not pending:
                    self._ledger.finish(run.run_id, _rows_status(rows), owner=run.lease_owner)
                    return ScheduledScanOutcome(
                        run=run,
                        claimed=True,
                        reason="already_complete",
                        symbols_total=len(rows),
                    )
            elif not rows:
                if "#manual-" in run.window_start:
                    self._ledger.finish(
                        run.run_id,
                        FAILED,
                        error_code="MANUAL_RUN_INTERRUPTED",
                        message="manual scan had no checkpoint to resume",
                        owner=run.lease_owner,
                    )
                    return ScheduledScanOutcome(run=run, claimed=False, reason="no_checkpoint")
                pending = starred
                if not pending:
                    self._ledger.finish(
                        run.run_id,
                        FAILED,
                        error_code="NO_STARRED_SYMBOLS",
                        message="no checkpoint and no starred symbols to resume",
                        owner=run.lease_owner,
                    )
                    return ScheduledScanOutcome(run=run, claimed=False, reason="no_checkpoint")
            else:
                # Pending = starred symbols that have no terminal row. A symbol
                # that never wrote a row (crash mid-batch) must still run; a
                # fully terminal batch must not re-run completed symbols.
                pending = _pending_symbols(run, rows, starred)
                if not pending:
                    self._ledger.finish(run.run_id, _rows_status(rows), owner=run.lease_owner)
                    return ScheduledScanOutcome(
                        run=run,
                        claimed=True,
                        reason="already_complete",
                        symbols_total=len(rows),
                    )
            outcome = self._execute(
                run,
                pending,
                period=run.period or period,
                trigger_source=run.trigger_source,
                source=run.source,
                metrics_only=run.metrics_only,
                provider=run.provider,
            )
            self._ledger.finish(
                run.run_id,
                _merged_status(outcome, self._container.store.list_scan_symbols(run.run_id)),
                owner=run.lease_owner,
            )
            return outcome
        except BaseException as exc:
            self._ledger.finish(
                run.run_id,
                FAILED,
                error_code=str(getattr(exc, "code", "JOB_FAILED")),
                message=str(exc),
                owner=run.lease_owner,
            )
            raise
        finally:
            stop.set()
            beat.join(timeout=1)
            self._ledger.release_scan_lock(run.lease_owner)

    def _heartbeat_loop(self, run_id: str, owner: str, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self._ledger.heartbeat(run_id, owner=owner)
                self._ledger.acquire_scan_lock(owner)
            except Exception as exc:  # noqa: BLE001 - log, keep trying while running
                import structlog

                structlog.get_logger(__name__).warning(
                    "job_heartbeat_failed", run_id=run_id, owner=owner, error=str(exc)
                )

    def run_manual(
        self,
        *,
        period: str = "4h",
        source: str = "backfill",
        metrics_only: bool = False,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> ScheduledScanOutcome:
        """Claim and run one manual batch over every starred symbol."""
        now = self._clock.now()
        batch_id = f"SCAN-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        claim = self._ledger.claim_scan(
            trigger_source="MANUAL",
            owner=batch_id,
            run_id=batch_id,
            source=source,
            metrics_only=metrics_only,
            provider=provider,
            period=period,
        )
        if not claim.claimed or claim.run is None:
            raise ScanAlreadyRunning(running_scan_id=claim.run_id)
        run = claim.run
        stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run.run_id, run.lease_owner, stop),
            daemon=True,
        )
        beat.start()
        try:
            symbols = tuple(contract.symbol for contract in self._container.store.list_starred())
            if not symbols:
                self._ledger.finish(
                    run.run_id,
                    FAILED,
                    error_code="NO_STARRED_SYMBOLS",
                    message="no starred symbols to scan",
                    owner=run.lease_owner,
                )
                return ScheduledScanOutcome(
                    run=run,
                    claimed=True,
                    reason="no_starred_symbols",
                    symbols_total=0,
                )
            outcome = self._execute(
                run,
                symbols,
                period=run.period or period,
                trigger_source="MANUAL",
                source=source,
                metrics_only=metrics_only,
                provider=provider,
                start=start,
                end=end,
            )
            self._ledger.finish(
                run.run_id,
                _merged_status(outcome, self._container.store.list_scan_symbols(run.run_id)),
                owner=run.lease_owner,
            )
            return outcome
        except BaseException as exc:
            self._ledger.finish(
                run.run_id,
                FAILED,
                error_code=str(getattr(exc, "code", "JOB_FAILED")),
                message=str(exc),
                owner=run.lease_owner,
            )
            raise
        finally:
            stop.set()
            beat.join(timeout=1)
            self._ledger.release_scan_lock(run.lease_owner)

    def _execute(
        self,
        run: JobRunRecord,
        symbols: tuple[str, ...],
        *,
        period: str,
        trigger_source: str = "SCHEDULED",
        source: str = "backfill",
        metrics_only: bool = False,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> ScheduledScanOutcome:
        scans: list[ScanRecord] = []
        failed: list[str] = []
        degraded: list[str] = []
        total_tokens = 0
        total_cost = Decimal("0")
        any_unpriced = False

        def _finish_symbol(
            symbol: str,
            started_at: str,
            *,
            stage: str,
            report_id: str | None,
            data_quality: str | None,
            error_code: str | None = None,
            error_message: str | None = None,
            failed_stage: str | None = None,
            llm_status: str | None = None,
            llm_tokens: int = 0,
            cost_usd: str = "0",
            cost_unpriced: bool = False,
            finished_at: str | None = None,
        ) -> None:
            self._container.store.upsert_scan_symbol(
                ScanSymbolRecord(
                    scan_id=run.run_id,
                    symbol=symbol,
                    stage=stage,
                    started_at=started_at,
                    finished_at=finished_at or utc_timestamp(self._clock.now()),
                    error_code=error_code,
                    error_message=error_message,
                    failed_stage=failed_stage,
                    report_id=report_id,
                    data_quality=data_quality,
                    llm_status=llm_status,
                    llm_tokens=llm_tokens,
                    cost_usd=cost_usd,
                    cost_unpriced=cost_unpriced,
                )
            )

        # A crash can commit a child scan/report and then lose the terminal
        # checkpoint write. Reuse the completed scan for that symbol instead of
        # creating a duplicate row and a second billed LLM call.
        completed = {
            child.symbol: child
            for child in self._container.store.list_scans_by_batch(run.run_id)
            if child.status == "DONE" and child.report_id
        }
        for symbol in symbols:
            started_at = utc_timestamp(self._clock.now())
            previous = completed.get(symbol)
            if previous is not None:
                scans.append(previous)
                previous_stage = "DEGRADED" if previous.llm_status == "REJECTED" else "DONE"
                if previous_stage == "DEGRADED":
                    degraded.append(symbol)
                total_tokens += previous.llm_tokens
                with suppress(InvalidOperation):
                    total_cost += Decimal(previous.cost_usd)
                if previous.llm_status is not None:
                    any_unpriced = any_unpriced or previous.cost_unpriced
                _finish_symbol(
                    symbol,
                    previous.started_at or started_at,
                    stage=previous_stage,
                    report_id=previous.report_id,
                    data_quality=previous.data_quality,
                    llm_status=previous.llm_status,
                    llm_tokens=previous.llm_tokens,
                    cost_usd=previous.cost_usd,
                    cost_unpriced=previous.cost_unpriced,
                    finished_at=previous.finished_at,
                )
                continue

            def _stage(name: str, symbol: str = symbol, started_at: str = started_at) -> None:
                self._container.store.upsert_scan_symbol(
                    ScanSymbolRecord(
                        scan_id=run.run_id,
                        symbol=symbol,
                        stage=name,
                        started_at=started_at,
                        finished_at=None,
                        error_code=None,
                        report_id=None,
                        data_quality=None,
                    )
                )

            _stage("QUEUED")
            try:
                result = ScanService(
                    container=self._container,  # type: ignore[arg-type]
                    lock=self._container.scan_lock,
                    on_stage=_stage,
                ).run(
                    ScanRequest(
                        symbol=symbol,
                        period=period,
                        trigger_source=trigger_source,
                        source=source,
                        metrics_only=metrics_only,
                        start=start,
                        end=end,
                        provider=provider,
                        batch_id=run.run_id,
                    )
                )
            except ScanError as exc:
                failed.append(symbol)
                _finish_symbol(
                    symbol,
                    started_at,
                    stage="FAILED",
                    report_id=None,
                    data_quality=None,
                    error_code=exc.code,
                    error_message=exc.message,
                    failed_stage=_failure_stage(exc, exc.code),
                )
            except Exception as exc:  # noqa: BLE001 - one symbol never aborts the batch
                failed.append(symbol)
                _finish_symbol(
                    symbol,
                    started_at,
                    stage="FAILED",
                    report_id=None,
                    data_quality=None,
                    error_code="SCAN_FAILED",
                    error_message=str(exc),
                    failed_stage=_failure_stage(exc, "SCAN_FAILED"),
                )
            else:
                scans.append(result.scan)
                symbol_stage = getattr(result, "symbol_stage", "DONE")
                llm_tokens = int(getattr(result, "llm_tokens", 0))
                cost_usd = str(getattr(result, "cost_usd", "0"))
                cost_unpriced = bool(getattr(result, "cost_unpriced", False))
                llm_state = getattr(result, "llm_state", {})
                if symbol_stage == "DEGRADED":
                    degraded.append(symbol)
                total_tokens += llm_tokens
                with suppress(InvalidOperation):
                    total_cost += Decimal(cost_usd)
                any_unpriced = any_unpriced or cost_unpriced
                _finish_symbol(
                    symbol,
                    started_at,
                    stage=symbol_stage,
                    report_id=getattr(result.report, "report_id", None),
                    data_quality=getattr(result.report, "data_quality", None),
                    llm_status=(
                        str(llm_state.get("validation_status"))
                        if isinstance(llm_state, dict) and llm_state.get("configured")
                        else None
                    ),
                    llm_tokens=llm_tokens,
                    cost_usd=cost_usd,
                    cost_unpriced=cost_unpriced,
                    finished_at=result.scan.finished_at,
                )
        return ScheduledScanOutcome(
            run=run,
            claimed=True,
            scans=tuple(scans),
            failed_symbols=tuple(failed),
            degraded_symbols=tuple(degraded),
            symbols_total=len(symbols),
            total_tokens=total_tokens,
            total_cost_usd=format(total_cost, "f"),
            cost_unpriced=any_unpriced,
        )


@dataclass(frozen=True)
class TriggerJobOutcome:
    """Result of one ``trigger_evaluate`` tick."""

    claimed: bool
    run: JobRunRecord | None = None
    counts: TriggerBatchOutcome = TriggerBatchOutcome()
    reason: str = ""
    error: str | None = None


def align_interval(instant: datetime, interval_seconds: int) -> datetime:
    """Floor an instant to the containing interval boundary in UTC.

    A naive instant is rejected rather than interpreted in the host local
    timezone: a silently shifted window would claim the wrong job run.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("trigger scheduling instants must be timezone-aware UTC")
    epoch = int(instant.astimezone(UTC).timestamp())
    return datetime.fromtimestamp((epoch // interval_seconds) * interval_seconds, tz=UTC)


def _counts_message(counts: TriggerBatchOutcome) -> str:
    return (
        f"open={counts.open} evaluated={counts.evaluated} "
        f"triggered={counts.triggered} skipped={counts.skipped} "
        f"stale={counts.stale} unavailable={counts.unavailable}"
    )


class TriggerJobRunner:
    """One interval tick: claim the current window, evaluate, finish the run.

    The ledger is the single source of truth. Only the current interval window
    is ever claimed, so a restart starts at the next boundary and missed
    windows are never replayed (ADR-0007). An overlapping tick loses the claim
    and records nothing new.
    """

    def __init__(
        self,
        *,
        service: TriggerService,
        ledger: JobLedger,
        clock: ClockPort,
        interval_seconds: int = DEFAULT_TRIGGER_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._service = service
        self._ledger = ledger
        self._clock = clock
        self._interval_seconds = interval_seconds

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    def run_tick(self, now: datetime | None = None) -> TriggerJobOutcome:
        instant = now or self._clock.now()
        window_start = utc_timestamp(align_interval(instant, self._interval_seconds))
        claim = self._ledger.claim(
            TRIGGER_JOB_TYPE,
            window_start,
            trigger_source="SCHEDULED",
            source=TRIGGER_JOB_SOURCE,
        )
        if not claim.claimed or claim.run is None:
            return TriggerJobOutcome(claimed=False, run=claim.run, reason=claim.reason)
        run = claim.run
        try:
            counts = self._service.evaluate_all(utc_timestamp(instant))
        except Exception as exc:  # noqa: BLE001 - a failed tick must not kill the daemon
            message = str(exc)
            # Log before finishing: a rejected or raising finish write must not
            # mask the original tick failure.
            _LOGGER.warning("trigger_job_failed", run_id=run.run_id, error=message)
            self._finish_safely(
                run,
                FAILED,
                error_code=str(getattr(exc, "code", "JOB_FAILED")),
                message=message,
            )
            failed = self._ledger.store.get_job_run(run.run_id) or run
            return TriggerJobOutcome(claimed=True, run=failed, reason="failed", error=message)
        finished_ok = self._finish_safely(run, DONE, message=_counts_message(counts))
        finished = self._ledger.store.get_job_run(run.run_id) or run
        return TriggerJobOutcome(
            claimed=True,
            run=finished,
            counts=counts,
            reason="done" if finished_ok else "finish_rejected",
        )

    def _finish_safely(
        self,
        run: JobRunRecord,
        status: str,
        *,
        error_code: str | None = None,
        message: str | None = None,
    ) -> bool:
        """Finish the run, never letting a ledger write replace the real error."""
        return _finish_run_safely(
            self._ledger,
            run,
            status,
            error_code=error_code,
            message=message,
            log_prefix="trigger_job",
        )


def _finish_run_safely(
    ledger: JobLedger,
    run: JobRunRecord,
    status: str,
    *,
    error_code: str | None = None,
    message: str | None = None,
    log_prefix: str = "job",
) -> bool:
    """Finish a run, never letting a ledger write replace the real outcome.

    This is the one finish policy shared by every runner; ``log_prefix`` keeps
    the historical per-job event names while the behaviour stays identical.
    """
    try:
        finished = ledger.finish(
            run.run_id,
            status,
            error_code=error_code,
            message=message,
            owner=run.lease_owner,
        )
    except Exception as exc:  # noqa: BLE001 - the tick outcome must survive
        _LOGGER.warning(
            f"{log_prefix}_finish_failed", run_id=run.run_id, status=status, error=str(exc)
        )
        return False
    if not finished:
        _LOGGER.warning(f"{log_prefix}_finish_rejected", run_id=run.run_id, status=status)
    return bool(finished)


@dataclass(frozen=True)
class PositionTickResult:
    """The outcome for one position inside a ``position_monitor`` tick."""

    position_id: str
    status: PositionTickStatus
    reason_code: str
    reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PositionTickStatus(self.status))
        if not self.reason_code.strip():
            raise ValueError("a position tick result requires a non-empty reason_code")

    @property
    def closed(self) -> bool:
        return self.status is PositionTickStatus.CLOSED


@dataclass(frozen=True)
class PositionMonitorOutcome:
    """Result of one ``position_monitor`` tick."""

    claimed: bool
    run: JobRunRecord | None = None
    positions_total: int = 0
    results: tuple[PositionTickResult, ...] = ()
    reason: str = ""
    reason_code: str = ""
    status: PositionMonitorStatus = PositionMonitorStatus.DONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PositionMonitorStatus(self.status))
        errored = any(item.status is PositionTickStatus.ERROR for item in self.results)
        if self.status is PositionMonitorStatus.DONE and errored:
            raise ValueError("a DONE position monitor outcome cannot contain ERROR results")
        if self.status is PositionMonitorStatus.PARTIAL and not errored:
            raise ValueError("a PARTIAL position monitor outcome requires an ERROR result")
        if self.status is PositionMonitorStatus.SKIPPED and self.claimed:
            raise ValueError("only a not-claimed tick may be SKIPPED")
        if self.status is PositionMonitorStatus.FAILED and not self.reason_code.strip():
            raise ValueError("a FAILED position monitor outcome requires a reason_code")

    @property
    def errors(self) -> tuple[str, ...]:
        """The position ids whose tick failed; derived, so it cannot disagree."""
        return tuple(
            item.position_id for item in self.results if item.status is PositionTickStatus.ERROR
        )

    @property
    def closed_position_ids(self) -> tuple[str, ...]:
        return tuple(item.position_id for item in self.results if item.closed)


def _position_counts_message(results: tuple[PositionTickResult, ...], total: int) -> str:
    closed = sum(1 for item in results if item.status == "CLOSED")
    stale = sum(1 for item in results if item.reason_code == "DATA_STALE")
    errors = sum(1 for item in results if item.status == "ERROR")
    no_action = sum(1 for item in results if item.status == "NO_ACTION")
    return f"open={total} closed={closed} no_action={no_action} stale={stale} errors={errors}"


class PositionMonitorRunner:
    """One code-only protective/stale tick over every OPEN position.

    The interval window is claimed once in the durable ledger, so an overlapping
    tick or a restart in the same window writes nothing. Only
    :meth:`PositionService.monitor` runs: no management service and no LLM port
    is reachable from this path.

    Side effect at the job boundary: every tick's per-position monitor first
    accrues the recorded funding settlements inside the holding window. That
    mutates ``funding``, writes funding events, and debits/credits the paper
    account cash before the stop rule is evaluated. The accrual is keyed by
    ``(position_id, settlement_time)``, so it is idempotent across ticks and
    restarts.
    """

    def __init__(
        self,
        positions: PositionService,
        ledger: JobLedger,
        clock: ClockPort,
        interval_seconds: int = DEFAULT_POSITION_MONITOR_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._positions = positions
        self._ledger = ledger
        self._clock = clock
        self._interval_seconds = interval_seconds

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    def run_tick(self, now: datetime | None = None) -> PositionMonitorOutcome:
        instant = now or self._clock.now()
        window_start = utc_timestamp(align_interval(instant, self._interval_seconds))
        claim = self._ledger.claim(
            POSITION_MONITOR_JOB_TYPE,
            window_start,
            trigger_source="SCHEDULED",
            source=POSITION_MONITOR_JOB_TYPE,
        )
        if not claim.claimed or claim.run is None:
            return PositionMonitorOutcome(
                claimed=False,
                run=claim.run,
                reason=claim.reason,
                status=PositionMonitorStatus.SKIPPED,
            )
        run = claim.run
        try:
            records = self._ledger.store.list_positions(state="OPEN", limit=None)
        except Exception as exc:  # noqa: BLE001 - a read failure must finish the run
            _finish_run_safely(
                self._ledger,
                run,
                FAILED,
                error_code="POSITION_READ_FAILED",
                message=str(exc),
            )
            finished = self._ledger.store.get_job_run(run.run_id) or run
            return PositionMonitorOutcome(
                claimed=True,
                run=finished,
                status=PositionMonitorStatus.FAILED,
                reason_code="POSITION_READ_FAILED",
                reason="position_read_failed",
            )

        results: list[PositionTickResult] = []
        for record in records:
            try:
                result = self._positions.monitor(record.position_id)
                # The conversion lives inside the guard too: an unexpected
                # source status becomes this position's ERROR instead of
                # aborting the tick and orphaning the RUNNING row (C2).
                results.append(self._tick_result(result))
            except Exception as exc:  # noqa: BLE001 - one position never aborts the tick
                results.append(
                    PositionTickResult(
                        record.position_id,
                        PositionTickStatus.ERROR,
                        "MONITOR_FAILED",
                        error=str(exc),
                    )
                )
                _LOGGER.warning(
                    "position_monitor_failed",
                    run_id=run.run_id,
                    position_id=record.position_id,
                    error=str(exc),
                )
                continue

        errors = tuple(
            item.position_id for item in results if item.status is PositionTickStatus.ERROR
        )
        status = PARTIAL if errors else DONE
        monitor_status = PositionMonitorStatus.PARTIAL if errors else PositionMonitorStatus.DONE
        _finish_run_safely(
            self._ledger,
            run,
            status,
            message=_position_counts_message(tuple(results), len(records)),
        )
        finished = self._ledger.store.get_job_run(run.run_id) or run
        return PositionMonitorOutcome(
            claimed=True,
            run=finished,
            positions_total=len(records),
            results=tuple(results),
            reason="partial" if errors else "done",
            status=monitor_status,
        )

    @staticmethod
    def _tick_result(result: PositionMonitorResult) -> PositionTickResult:
        reason_code = PROTECTIVE_STOP if result.status == "CLOSED" else result.reason_code
        return PositionTickResult(
            position_id=result.position_id,
            status=PositionTickStatus(result.status),
            reason_code=reason_code,
            reason=result.reason,
        )


@dataclass(frozen=True)
class ManagementPositionOutcome:
    """The per-position outcome of one management cadence window."""

    position_id: str
    status: ManagementPositionStatus
    reason_code: str = ""
    message: str | None = None
    action: str | None = None
    claimed: bool = True
    # The typed refusal code when ``status`` is REFUSED; ``None`` for a hard
    # failure, a skip, or a completed decision.
    refusal_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ManagementPositionStatus(self.status))
        if not self.reason_code.strip():
            raise ValueError("a management position outcome requires a reason_code")
        if self.status is ManagementPositionStatus.REFUSED and self.refusal_code is None:
            object.__setattr__(self, "refusal_code", self.reason_code)
        if self.status is not ManagementPositionStatus.REFUSED and self.refusal_code is not None:
            raise ValueError("only a REFUSED outcome carries a refusal_code")


@dataclass(frozen=True)
class ManagementJobOutcome:
    """Result of one ``position_manage`` cadence window."""

    window_start: datetime
    window_end: datetime
    results: tuple[ManagementPositionOutcome, ...] = ()
    status: ManagementWindowStatus = ManagementWindowStatus.DONE
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ManagementWindowStatus(self.status))


class ManagementJobRunner:
    """One scan-aligned management window: one decision per position per window.

    Each ``(position, window)`` is claimed under its own ledger key, so an
    overlapping tick, a restart, or a second runner at the same clock cannot ask
    the LLM or write a second order. The deterministic idempotency key
    ``auto-{period}-{window_start}`` makes a reclaimed window reuse the stored
    decision.

    ADR-0007 requires one management run for the open positions after a restart;
    the daemon performs that catch-up explicitly in addition to the periodic
    cadence. Missed *periodic* windows are still never replayed: only the
    window containing the supplied instant is ever claimed, and a caller-supplied
    ``window_start`` that is not the current window is refused without claiming.
    """

    def __init__(
        self,
        service: ManagementService,
        ledger: JobLedger,
        clock: ClockPort,
        *,
        period: str,
        funding_settlement_enabled: bool = True,
        risk_breach_enabled: bool = True,
    ) -> None:
        self._service = service
        self._ledger = ledger
        self._clock = clock
        self._period = validate_period(period)
        self._funding_settlement_enabled = funding_settlement_enabled
        self._risk_breach_enabled = risk_breach_enabled

    @property
    def period(self) -> str:
        return self._period

    def run_window(
        self, now: datetime | None = None, *, window_start: datetime | None = None
    ) -> ManagementJobOutcome:
        instant = now or self._clock.now()
        current = align_to_boundary(instant, self._period)
        if window_start is not None:
            if window_start.tzinfo is None or window_start.utcoffset() is None:
                raise ValueError("window_start must be timezone-aware UTC")
            supplied = window_start.astimezone(UTC)
            if supplied != current:
                # A stale/foreign window can no longer act: the ledger is not
                # touched, so a missed window is never replayed (M9).
                return ManagementJobOutcome(
                    window_start=supplied,
                    window_end=supplied + timedelta(hours=PERIOD_HOURS[self._period]),
                    status=ManagementWindowStatus.SKIPPED,
                    reason="window_not_current",
                )
        window_end = current + timedelta(hours=PERIOD_HOURS[self._period])
        window_start_text = utc_timestamp(current)
        window_end_text = utc_timestamp(window_end)
        now_text = utc_timestamp(instant)
        try:
            records = self._ledger.store.list_positions(state="OPEN", limit=None)
        except Exception as exc:  # noqa: BLE001 - the window outcome must survive
            # No row is claimed yet, so this failure cannot orphan a RUNNING row.
            _LOGGER.warning("management_window_position_read_failed", error=str(exc))
            return ManagementJobOutcome(
                window_start=current,
                window_end=window_end,
                status=ManagementWindowStatus.FAILED,
                reason="position_read_failed",
            )
        if not records:
            return self._empty_window(current, window_end, window_start_text)
        # One account-level daily-loss measurement for the whole window; the
        # per-position evaluations reuse it instead of rescanning the journal.
        # The measurement is only needed when it can actually be consumed, and a
        # failure must not cost an otherwise usable cadence window. No provider
        # means no decision can be made, so no read is attempted at all (H3).
        risk_context = ManagementRiskContext.unmeasured()
        if self._risk_breach_enabled and self._service.configured:
            try:
                risk_context = self._service.risk_context(now=now_text, open_records=records)
            except Exception as exc:  # noqa: BLE001 - an optional measure never aborts
                _LOGGER.warning("management_window_risk_context_failed", error=str(exc))
                risk_context = ManagementRiskContext.unmeasured(
                    "the account-level risk measurement failed"
                )
        results = tuple(
            self._run_position(
                record.position_id,
                window_start=current,
                window_end=window_end,
                window_start_text=window_start_text,
                window_end_text=window_end_text,
                now_text=now_text,
                risk_context=risk_context,
            )
            for record in records
        )
        status = self._aggregate_status(results)
        reason = "done" if status is ManagementWindowStatus.DONE else status.value.lower()
        return ManagementJobOutcome(
            window_start=current,
            window_end=window_end,
            results=results,
            status=status,
            reason=reason,
        )

    @staticmethod
    def _aggregate_status(
        results: tuple[ManagementPositionOutcome, ...],
    ) -> ManagementWindowStatus:
        """A hard failure dominates a refusal, which dominates a completed row."""
        statuses = {item.status for item in results}
        if ManagementPositionStatus.FAILED in statuses:
            return ManagementWindowStatus.FAILED
        if ManagementPositionStatus.REFUSED in statuses:
            return ManagementWindowStatus.PARTIAL
        if ManagementPositionStatus.DONE in statuses:
            return ManagementWindowStatus.DONE
        return ManagementWindowStatus.SKIPPED

    def _empty_window(
        self, window_start: datetime, window_end: datetime, window_start_text: str
    ) -> ManagementJobOutcome:
        """Record one visible window run when there are no open positions."""
        claim = self._ledger.claim(
            POSITION_MANAGE_JOB_TYPE,
            window_start_text,
            trigger_source="SCHEDULED",
            source=POSITION_MANAGE_SOURCE,
            period=self._period,
        )
        if claim.claimed and claim.run is not None:
            _finish_run_safely(
                self._ledger,
                claim.run,
                DONE,
                message="no open positions",
            )
        return ManagementJobOutcome(
            window_start=window_start,
            window_end=window_end,
            status=ManagementWindowStatus.DONE,
            reason="no_open_positions" if claim.claimed else claim.reason,
        )

    def _run_position(
        self,
        position_id: str,
        *,
        window_start: datetime,
        window_end: datetime,
        window_start_text: str,
        window_end_text: str,
        now_text: str,
        risk_context: ManagementRiskContext,
    ) -> ManagementPositionOutcome:
        key = f"{window_start_text}#{position_id}"
        claim = self._ledger.claim(
            POSITION_MANAGE_JOB_TYPE,
            key,
            trigger_source="SCHEDULED",
            source=POSITION_MANAGE_SOURCE,
            period=self._period,
        )
        if not claim.claimed or claim.run is None:
            return ManagementPositionOutcome(
                position_id, ManagementPositionStatus.SKIPPED, claim.reason, claimed=False
            )
        run = claim.run
        # No provider can decide anything: skip before the funding/trade-log/mark
        # reads so an unconfigured daemon does no per-window measurement work.
        if not self._service.configured:
            message = "no management LLM provider is configured"
            return self._finish_position(
                run,
                position_id,
                ManagementPositionStatus.SKIPPED,
                reason_code="NO_LLM_PROVIDER",
                message=message,
                ledger_status=SKIPPED,
                ledger_error_code="NO_LLM_PROVIDER",
            )
        try:
            due = self._service.due_conditions(
                position_id,
                window_start=window_start_text,
                window_end=window_end_text,
                now=now_text,
                funding_settlement_enabled=self._funding_settlement_enabled,
                risk_breach_enabled=self._risk_breach_enabled,
                risk_context=risk_context,
            )
            if not due.due:
                return self._finish_position(
                    run,
                    position_id,
                    ManagementPositionStatus.SKIPPED,
                    reason_code="NOT_DUE",
                    message=due.reason,
                    ledger_status=SKIPPED,
                    ledger_error_code="NOT_DUE",
                )
            result = self._run_management(position_id, window_start_text, conditions=due.conditions)
        except ManagementRejected as exc:
            if exc.code in _TRANSIENT_MANAGEMENT_CODES:
                # An honest retry: a transient provider/infra failure is not a
                # terminal refusal, so the row and the outcome are FAILED (M15).
                _LOGGER.warning(
                    "management_job_transient_failure",
                    run_id=run.run_id,
                    position_id=position_id,
                    error_code=exc.code,
                )
                return self._finish_position(
                    run,
                    position_id,
                    ManagementPositionStatus.FAILED,
                    reason_code=exc.code,
                    message=exc.message,
                    ledger_status=FAILED,
                    ledger_error_code=exc.code,
                )
            # A typed decision refusal is a recorded per-position outcome, not a
            # crash: REFUSED with its code, distinct from FAILED.
            return self._finish_position(
                run,
                position_id,
                ManagementPositionStatus.REFUSED,
                reason_code=exc.code,
                message=exc.message,
                ledger_status=DONE,
                ledger_error_code=exc.code,
            )
        except PositionCloseError as exc:
            # Every code is logged. Only routine stale/invalid-state refusals
            # skip; gate/broker refusals are REFUSED; everything else (a
            # broken read, a conflict) is a hard failure, never a silent skip.
            _LOGGER.warning(
                "management_position_closed_error",
                run_id=run.run_id,
                position_id=position_id,
                error_code=exc.code,
            )
            if exc.code in _ROUTINE_POSITION_SKIP_CODES:
                return self._finish_position(
                    run,
                    position_id,
                    ManagementPositionStatus.SKIPPED,
                    reason_code=exc.code,
                    message=exc.message,
                    ledger_status=SKIPPED,
                    ledger_error_code=exc.code,
                )
            if exc.code in _POSITION_REFUSAL_CODES:
                return self._finish_position(
                    run,
                    position_id,
                    ManagementPositionStatus.REFUSED,
                    reason_code=exc.code,
                    message=exc.message,
                    ledger_status=DONE,
                    ledger_error_code=exc.code,
                )
            return self._finish_position(
                run,
                position_id,
                ManagementPositionStatus.FAILED,
                reason_code=exc.code,
                message=exc.message,
                ledger_status=FAILED,
                ledger_error_code=exc.code,
            )
        except Exception as exc:  # noqa: BLE001 - one position never aborts the window
            _LOGGER.warning(
                "management_job_failed",
                run_id=run.run_id,
                position_id=position_id,
                error=str(exc),
            )
            return self._finish_position(
                run,
                position_id,
                ManagementPositionStatus.FAILED,
                reason_code="JOB_FAILED",
                message=str(exc),
                ledger_status=FAILED,
                ledger_error_code="JOB_FAILED",
            )
        conditions_text = ",".join(item.value for item in due.conditions)
        message = (
            f"action={result.action.value} status={result.status.value} "
            f"executed={str(result.executed).lower()} conditions={conditions_text}"
        )
        if result.status is ManagementStatus.BLOCKED:
            # A gate block is a typed refusal: the decision ran and is recorded,
            # but no order was authorized.
            return self._finish_position(
                run,
                position_id,
                ManagementPositionStatus.REFUSED,
                reason_code=result.reason_code,
                message=message,
                action=result.action.value,
                ledger_status=DONE,
                ledger_error_code=result.reason_code,
            )
        return self._finish_position(
            run,
            position_id,
            ManagementPositionStatus.DONE,
            reason_code=result.reason_code,
            message=message,
            action=result.action.value,
            ledger_status=DONE,
        )

    def _finish_position(
        self,
        run: JobRunRecord,
        position_id: str,
        status: ManagementPositionStatus,
        *,
        reason_code: str,
        message: str | None,
        action: str | None = None,
        ledger_status: str = DONE,
        ledger_error_code: str | None = None,
    ) -> ManagementPositionOutcome:
        """Finish one claimed per-position row and return its closed outcome."""
        _finish_run_safely(
            self._ledger,
            run,
            ledger_status,
            error_code=ledger_error_code,
            message=message,
        )
        return ManagementPositionOutcome(
            position_id,
            status,
            reason_code,
            message=message,
            action=action,
            refusal_code=reason_code if status is ManagementPositionStatus.REFUSED else None,
        )

    def _run_management(
        self,
        position_id: str,
        window_start_text: str,
        *,
        conditions: tuple[ManagementCondition, ...] = (),
    ) -> ManagementResult:
        return self._service.manage(
            position_id,
            idempotency_key=f"auto-{self._period}-{window_start_text}",
            conditions=conditions,
        )


# Worst first: the aggregate status of a window is its most severe row. A window
# that did real work reports DONE over SKIPPED (M18).
JOB_STATUS_PRECEDENCE = (FAILED, PARTIAL, RUNNING, DONE, SKIPPED)


@dataclass(frozen=True)
class JobSummary:
    """The aggregated status of the latest boundary window for a job type.

    ``position_manage`` writes one row per ``"{window}#{position_id}"`` key, so
    the newest single row is not the window status. This summary groups the
    rows of the newest boundary and surfaces the boundary (the part before
    ``#``) so ``window_start`` keeps its ISO meaning.
    """

    job_type: str
    status: str
    window_start: str
    finished_at: str | None
    message: str | None
    error_code: str | None

    def to_payload(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "window_start": self.window_start,
            "finished_at": self.finished_at or "",
            "message": self.message,
            "error_code": self.error_code,
        }


def _ledger_window_boundary(window_start: str) -> str:
    """The boundary window of a ledger key (the part before ``#``)."""
    return window_start.split("#", 1)[0]


def _effective_status(run: JobRunRecord) -> str:
    """A typed per-position refusal degrades a DONE row to partial success."""
    if run.status == DONE and run.error_code:
        return PARTIAL
    return run.status


def latest_job_summary(store: StorePort, job_type: str, *, limit: int = 200) -> JobSummary | None:
    """Aggregate the latest boundary window across its per-position rows.

    Status precedence is ``FAILED > PARTIAL > RUNNING > DONE > SKIPPED``: a
    window that did real work reports DONE over SKIPPED. A DONE row carrying a
    typed ``error_code`` (a recorded per-position refusal) counts as
    ``PARTIAL``. The surfaced ``window_start`` is the boundary ISO timestamp,
    never the internal ``"<ts>#<position_id>"`` key.
    """
    runs = store.list_job_runs(job_type, limit=limit)
    if not runs:
        return None
    boundary = _ledger_window_boundary(runs[0].window_start)
    window_runs = [run for run in runs if _ledger_window_boundary(run.window_start) == boundary]
    statuses = {_effective_status(run) for run in window_runs}
    status = next(
        (item for item in JOB_STATUS_PRECEDENCE if item in statuses),
        _effective_status(window_runs[0]),
    )
    representative = next(
        (run for run in window_runs if _effective_status(run) == status), window_runs[0]
    )
    finished_at = max(
        (run.finished_at for run in window_runs if run.finished_at),
        default=None,
    )
    error_code = representative.error_code or next(
        (run.error_code for run in window_runs if run.error_code), None
    )
    return JobSummary(
        job_type=job_type,
        status=status,
        window_start=boundary,
        finished_at=finished_at,
        message=representative.message,
        error_code=error_code,
    )


def job_summary_payload(summary: JobSummary | None) -> dict[str, str | None] | None:
    """The additive ``jobs`` payload shape, or ``None`` when the job never ran."""
    return None if summary is None else summary.to_payload()


def latest_jobs_payload(store: StorePort) -> dict[str, dict[str, str | None] | None]:
    """The latest-window status of the slice-26 jobs, shared by API and CLI.

    One store read path means the HTTP status and ``oft daemon status`` cannot
    disagree about a window (M12).
    """
    return {
        "position_monitor": job_summary_payload(
            latest_job_summary(store, POSITION_MONITOR_JOB_TYPE)
        ),
        "position_manage": job_summary_payload(latest_job_summary(store, POSITION_MANAGE_JOB_TYPE)),
    }


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POSITION_MONITOR_INTERVAL_SECONDS",
    "DEFAULT_TRIGGER_INTERVAL_SECONDS",
    "DONE",
    "FAILED",
    "JOB_STATUS_PRECEDENCE",
    "JobClaim",
    "JobLedger",
    "JobSummary",
    "ManagementCondition",
    "ManagementJobOutcome",
    "ManagementJobRunner",
    "ManagementPositionOutcome",
    "ManagementPositionStatus",
    "ManagementWindowStatus",
    "MISSED",
    "PARTIAL",
    "POSITION_MANAGE_JOB_TYPE",
    "POSITION_MANAGE_SOURCE",
    "POSITION_MONITOR_JOB_TYPE",
    "PROTECTIVE_STOP",
    "PositionMonitorOutcome",
    "PositionMonitorRunner",
    "PositionMonitorStatus",
    "PositionTickResult",
    "PositionTickStatus",
    "RestartState",
    "RUNNING",
    "SCAN_JOB_TYPE",
    "SCAN_LOCK_NAME",
    "ScheduledScanOutcome",
    "ScheduledScanRunner",
    "TRIGGER_JOB_SOURCE",
    "TRIGGER_JOB_TYPE",
    "TriggerJobOutcome",
    "TriggerJobRunner",
    "align_interval",
    "job_summary_payload",
    "latest_jobs_payload",
    "latest_job_summary",
]
