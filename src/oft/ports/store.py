"""Store port: transactional state and the immutable-part manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from oft.domain.data_quality import DataGap
from oft.domain.symbols import SymbolContract


class StoreUnavailableError(Exception):
    """A store read failed; surfaced as the catalog ``DATA_UNAVAILABLE`` code."""

    code = "DATA_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LeaseLostError(Exception):
    """Another run owns the lease; the current worker must stop publishing."""

    code = "BACKFILL_ALREADY_RUNNING"

    def __init__(self, message: str = "backfill lease lost") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SymbolUpsertResult:
    """Row counts from one idempotent symbol-registry ingest."""

    inserted: int
    updated: int
    unchanged: int
    total: int


@dataclass(frozen=True)
class ManifestEntry:
    """One published Parquet part."""

    path: str
    data_type: str
    symbol: str
    date: str
    rows: int
    bytes: int
    checksum: str
    source: str
    created_at: str
    status: str = "PUBLISHED"
    metric_version: str | None = None
    input_manifest_hash: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    run_id: str | None = None
    last_n: int | None = None
    recomputed_from: str | None = None
    parent_run_id: str | None = None
    run_seq: int = 0


@dataclass(frozen=True)
class ScanRecord:
    """A completed scan and its deterministic summary values."""

    scan_id: str
    symbol: str
    period: str
    status: str
    trigger_source: str
    metrics_only: bool
    fixture_path: str
    started_at: str
    finished_at: str
    window_start: str
    window_end: str
    trade_count: int
    buy_volume: str
    sell_volume: str
    delta: str
    cvd: str
    data_quality: str
    manifest_path: str
    parquet_rows: int
    created_at: str
    report_id: str | None = None
    source_mode: str = "FIXTURE"
    metrics_manifest_json: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ReportRecord:
    """A CALC-only report: metric manifest plus the frozen snapshot document."""

    report_id: str
    scan_id: str
    symbol: str
    timeframe: str
    generated_at: str
    metrics_manifest_json: str
    llm_decision_id: str | None
    narrative_ref: str | None
    data_quality: str
    snapshot_json: str
    metrics_json: str


@dataclass(frozen=True)
class IdempotencyRecord:
    """A stored idempotent response for one ``(scope, key)`` pair."""

    scope: str
    key: str
    request_hash: str
    response_status: int
    response_json: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class BackfillRunRecord:
    """One orchestrator-visible backfill run."""

    run_id: str
    symbol: str
    data_type: str
    start_date: str
    end_date: str
    status: str
    started_at: str
    finished_at: str | None
    estimated_bytes: int
    processed_bytes: int
    files_total: int
    files_verified: int
    files_failed: int
    error_code: str | None
    message: str | None


@dataclass(frozen=True)
class BackfillManifestRow:
    """Per-file backfill state: URL, checksum, bytes, rows, status, verified_at."""

    file_key: str
    run_id: str | None
    data_type: str
    symbol: str
    date: str
    source_url: str
    checksum: str
    checksum_verified: bool
    status: str
    bytes: int
    rows: int
    downloaded_at: str | None
    verified_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class JobRunRecord:
    """One durable scheduler job run (periodic window or manual trigger)."""

    run_id: str
    job_type: str
    window_start: str
    status: str
    trigger_source: str
    started_at: str
    finished_at: str | None
    error_code: str | None
    message: str | None
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str


@dataclass(frozen=True)
class JobLockRecord:
    """A named, leased advisory lock (``backfill``, ``scan``, ...)."""

    lock_name: str
    owner: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class ScanSymbolRecord:
    """Per-symbol stage checkpoint inside a scan job."""

    scan_id: str
    symbol: str
    stage: str
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    report_id: str | None
    data_quality: str | None


class StorePort(Protocol):
    """Persistence contract shared by the API and CLI diagnostics."""

    def initialize(self) -> None: ...

    def save_scan(self, record: ScanRecord) -> None: ...

    def update_scan(self, record: ScanRecord) -> None: ...

    def save_scan_with_manifest(self, record: ScanRecord, entry: ManifestEntry) -> None: ...

    def save_report_and_complete_scan(self, report: ReportRecord, scan: ScanRecord) -> None: ...

    def fail_stale_scans(self, *, now: str, older_than_seconds: int = 900) -> int: ...

    def get_scan(self, scan_id: str) -> ScanRecord | None: ...

    def latest_scan(self) -> ScanRecord | None: ...

    def list_scans(self, limit: int = 20, offset: int = 0) -> tuple[ScanRecord, ...]: ...

    def count_scans(self) -> int: ...

    # -- Reports ---------------------------------------------------------

    def save_report(self, record: ReportRecord) -> None: ...

    def get_report(self, report_id: str) -> ReportRecord | None: ...

    def list_reports(
        self, *, scan_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[ReportRecord, ...]: ...

    def count_reports(self, *, scan_id: str | None = None) -> int: ...

    # -- Idempotency -----------------------------------------------------

    def get_idempotency(self, *, scope: str, key: str) -> IdempotencyRecord | None: ...

    def reserve_idempotency(self, record: IdempotencyRecord) -> bool: ...

    def renew_idempotency(self, *, scope: str, key: str, expires_at: str) -> None: ...

    def complete_idempotency(self, record: IdempotencyRecord) -> None: ...

    def delete_idempotency(self, *, scope: str, key: str) -> None: ...

    def purge_expired_idempotency(self, *, now: str) -> int: ...

    def save_idempotency(self, record: IdempotencyRecord) -> None: ...

    def record_manifest(self, entry: ManifestEntry) -> None: ...

    def upsert_manifest_entry(self, entry: ManifestEntry) -> None: ...

    def upsert_manifest_entries(self, entries: tuple[ManifestEntry, ...]) -> None: ...

    def delete_manifest_entry(self, path: str) -> None: ...

    def get_manifest_entry(self, path: str) -> ManifestEntry | None: ...

    def list_manifest(
        self, *, symbol: str | None = None, date: str | None = None
    ) -> tuple[ManifestEntry, ...]: ...
    def integrity_check(self) -> bool: ...

    def upsert_symbols(
        self, contracts: tuple[SymbolContract, ...], *, observed_at: str
    ) -> SymbolUpsertResult: ...

    def list_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        starred: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]: ...

    def count_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        starred: bool | None = None,
    ) -> int: ...

    def get_symbol(self, symbol: str) -> SymbolContract | None: ...

    def set_starred(self, symbol: str, starred: bool, *, starred_at: str | None = None) -> bool: ...

    def list_starred(self) -> tuple[SymbolContract, ...]: ...

    # -- Backfill state --------------------------------------------------

    def start_backfill_run(
        self,
        record: BackfillRunRecord,
        *,
        owner: str | None = None,
        now: str | None = None,
    ) -> bool: ...

    def touch_backfill_run(
        self, run_id: str, *, updated_at: str, owner: str | None = None
    ) -> bool: ...

    def save_backfill_run(self, record: BackfillRunRecord, *, owner: str | None = None) -> bool: ...

    def get_backfill_run(self, run_id: str) -> BackfillRunRecord | None: ...

    def latest_backfill_run(self) -> BackfillRunRecord | None: ...

    def list_backfill_runs(self, limit: int = 50) -> tuple[BackfillRunRecord, ...]: ...

    def upsert_backfill_manifest(self, row: BackfillManifestRow) -> None: ...

    def get_backfill_manifest(self, file_key: str) -> BackfillManifestRow | None: ...

    def list_backfill_manifest(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        run_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[BackfillManifestRow, ...]: ...

    def commit_backfill_file(
        self,
        *,
        row: BackfillManifestRow,
        entries: tuple[ManifestEntry, ...],
        resolved_at: str,
        owner: str | None = None,
        replace_date: bool = False,
    ) -> None: ...

    def discard_backfill_file(
        self,
        *,
        file_key: str,
        entries: tuple[ManifestEntry, ...],
        error_code: str,
        owner: str | None = None,
        run_id: str | None = None,
    ) -> None: ...

    def upsert_data_gap(self, gap: DataGap) -> None: ...

    def resolve_data_gaps_for_date(
        self,
        *,
        symbol: str,
        data_type: str,
        day: str,
        resolved_at: str,
        resolution: str = "BACKFILL_RESOLVED",
    ) -> None: ...

    def list_data_gaps(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[DataGap, ...]: ...

    # -- Durable job ledger ----------------------------------------------

    def claim_job_run(self, record: JobRunRecord) -> tuple[bool, JobRunRecord | None, bool]: ...

    def finish_job_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: str,
        error_code: str | None = None,
        message: str | None = None,
    ) -> bool: ...

    def delete_job_run(self, run_id: str) -> bool: ...

    def renew_job_lease(
        self, run_id: str, *, owner: str, heartbeat_at: str, lease_expires_at: str
    ) -> bool: ...

    def get_job_run(self, run_id: str) -> JobRunRecord | None: ...

    def get_job_run_by_window(self, job_type: str, window_start: str) -> JobRunRecord | None: ...

    def latest_job_run(self, job_type: str) -> JobRunRecord | None: ...

    def list_job_runs(
        self, job_type: str | None = None, limit: int = 50
    ) -> tuple[JobRunRecord, ...]: ...

    def acquire_job_lock(
        self, lock_name: str, owner: str, *, acquired_at: str, expires_at: str
    ) -> bool: ...

    def release_job_lock(self, lock_name: str, owner: str) -> bool: ...

    def get_job_lock(self, lock_name: str) -> JobLockRecord | None: ...

    def upsert_scan_symbol(self, record: ScanSymbolRecord) -> None: ...

    def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]: ...
