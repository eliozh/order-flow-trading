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


@dataclass(frozen=True)
class ScanRecord:
    """A completed fixture scan and its golden delta value."""

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


class StorePort(Protocol):
    """Persistence contract shared by the API and CLI diagnostics."""

    def initialize(self) -> None: ...

    def save_scan(self, record: ScanRecord) -> None: ...

    def save_scan_with_manifest(self, record: ScanRecord, entry: ManifestEntry) -> None: ...

    def get_scan(self, scan_id: str) -> ScanRecord | None: ...

    def latest_scan(self) -> ScanRecord | None: ...

    def list_scans(self, limit: int = 20) -> tuple[ScanRecord, ...]: ...

    def record_manifest(self, entry: ManifestEntry) -> None: ...

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
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]: ...

    def count_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
    ) -> int: ...

    def get_symbol(self, symbol: str) -> SymbolContract | None: ...

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
    ) -> None: ...

    def discard_backfill_file(
        self,
        *,
        file_key: str,
        entries: tuple[ManifestEntry, ...],
        error_code: str,
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
