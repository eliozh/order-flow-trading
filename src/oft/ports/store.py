"""Store port: transactional state and the immutable-part manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class StorePort(Protocol):
    """Persistence contract shared by the API and CLI diagnostics."""

    def initialize(self) -> None: ...

    def save_scan(self, record: ScanRecord) -> None: ...

    def save_scan_with_manifest(self, record: ScanRecord, entry: ManifestEntry) -> None: ...

    def get_scan(self, scan_id: str) -> ScanRecord | None: ...

    def latest_scan(self) -> ScanRecord | None: ...

    def list_scans(self, limit: int = 20) -> tuple[ScanRecord, ...]: ...

    def record_manifest(self, entry: ManifestEntry) -> None: ...

    def list_manifest(self) -> tuple[ManifestEntry, ...]: ...

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
