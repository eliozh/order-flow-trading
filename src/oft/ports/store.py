"""Store port: transactional state and the immutable-part manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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
    batch_id: str | None = None
    llm_configured: bool = False
    llm_status: str | None = None
    llm_tokens: int = 0
    cost_usd: str = "0"
    cost_unpriced: bool = True


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
    llm_json: str | None = None
    window_policy_json: str | None = None


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
    source: str = "backfill"
    metrics_only: bool = False
    provider: str | None = None
    symbol: str | None = None
    period: str | None = None


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
    error_message: str | None = None
    llm_status: str | None = None
    llm_tokens: int = 0
    cost_usd: str = "0"
    cost_unpriced: bool = True


@dataclass(frozen=True)
class LLMCallRecord:
    """One provider call attempt, including the repair attempt (``attempt=2``)."""

    call_id: str
    role: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: int
    prompt_hash: str
    input_hash: str
    attempt: int
    request_id: str
    status: str
    raw_output_hash: str
    usage_json: str
    latency_ms: int
    created_at: str
    decision_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DecisionRecord:
    """The persisted ``llm-decision`` envelope plus query keys."""

    decision_id: str
    role: str
    decision_type: str
    validation_status: str
    raw_output_hash: str
    persisted_at: str
    payload_json: str


@dataclass(frozen=True)
class BudgetRecord:
    """One per-call budget ledger row (rollups are derived, never hand-edited)."""

    record_id: str
    call_id: str
    decision_id: str | None
    occurred_at: str
    provider: str
    model: str
    cost_usd: str
    price_version: str
    unpriced: bool
    budget_state: str
    period_day: str
    payload_json: str


@dataclass(frozen=True)
class OpportunityRecord:
    """The persisted ``opportunity`` contract envelope plus query keys."""

    opportunity_id: str
    symbol: str
    state: str
    created_at: str
    updated_at: str
    payload_json: str


@dataclass(frozen=True)
class OpportunityEventRecord:
    """One append-only state history entry."""

    opportunity_id: str
    seq: int
    at: str
    from_state: str | None
    to_state: str
    reason_code: str
    note: str


@dataclass(frozen=True)
class TriggerEventRecord:
    """The persisted ``trigger-event`` contract envelope plus query keys."""

    trigger_id: str
    opportunity_id: str
    symbol: str
    status: str
    idempotency_key: str
    triggered_at: str
    payload_json: str


@dataclass(frozen=True)
class RiskVerdictRecord:
    """A persisted deterministic risk verdict."""

    risk_verdict_id: str
    trigger_id: str
    decision_id: str
    verdict: str
    binding_cap: str
    computed_at: str
    expires_at: str
    payload_json: str


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    client_order_id: str
    environment: str
    symbol: str
    status: str
    risk_verdict_id: str
    idempotency_key: str
    created_at: str
    updated_at: str
    payload_json: str


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    order_id: str
    environment: str
    symbol: str
    filled_at: str
    payload_json: str


@dataclass(frozen=True)
class PositionRecord:
    position_id: str
    account_id: str
    environment: str
    symbol: str
    state: str
    opened_at: str
    payload_json: str


@dataclass(frozen=True)
class PaperAccountRecord:
    account_id: str
    environment: str
    currency: str
    initial_balance: str
    balance: str
    updated_at: str


@dataclass(frozen=True)
class AuditEventRecord:
    event_id: str
    seq: int
    event_type: str
    created_at: str
    previous_hash: str
    event_hash: str
    payload_json: str


@dataclass(frozen=True)
class AuditEventDraft:
    """An audit event before the store assigns its chained seq/hash."""

    event_type: str
    actor: dict[str, Any]
    subject: dict[str, Any]
    payload: dict[str, Any]
    created_at: str


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

    def list_scans_by_batch(self, batch_id: str) -> tuple[ScanRecord, ...]: ...

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
        lease_owner: str | None = None,
    ) -> bool: ...

    def fail_stale_job_runs(self, *, job_type: str, now: str) -> int: ...

    def delete_job_run(self, run_id: str) -> bool: ...

    def renew_job_lease(
        self, run_id: str, *, owner: str, heartbeat_at: str, lease_expires_at: str
    ) -> bool: ...

    def get_job_run(self, run_id: str) -> JobRunRecord | None: ...

    def get_job_run_by_window(self, job_type: str, window_start: str) -> JobRunRecord | None: ...

    def latest_job_run(self, job_type: str) -> JobRunRecord | None: ...

    def list_job_runs(
        self, job_type: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[JobRunRecord, ...]: ...

    def count_job_runs(self, job_type: str | None = None) -> int: ...

    def list_stale_job_runs(self, *, now: str) -> tuple[JobRunRecord, ...]: ...

    def acquire_job_lock(
        self, lock_name: str, owner: str, *, acquired_at: str, expires_at: str
    ) -> bool: ...

    def release_job_lock(self, lock_name: str, owner: str) -> bool: ...

    def get_job_lock(self, lock_name: str) -> JobLockRecord | None: ...

    def upsert_scan_symbol(self, record: ScanSymbolRecord) -> None: ...

    def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]: ...

    # -- LLM decision ledger ---------------------------------------------

    def save_llm_call(self, record: LLMCallRecord) -> None: ...

    def list_llm_calls(
        self, *, decision_id: str | None = None, role: str | None = None
    ) -> tuple[LLMCallRecord, ...]: ...

    def save_decision(self, record: DecisionRecord) -> None: ...

    def get_decision(self, decision_id: str) -> DecisionRecord | None: ...

    def list_decisions(self, *, role: str | None = None) -> tuple[DecisionRecord, ...]: ...

    # -- Budget ledger ---------------------------------------------------

    def save_budget_record(self, record: BudgetRecord) -> None: ...

    def list_budget_records(
        self, *, decision_id: str | None = None
    ) -> tuple[BudgetRecord, ...]: ...

    # -- Opportunity ledger ----------------------------------------------

    def save_opportunity(self, record: OpportunityRecord) -> None: ...

    def transition_opportunity(
        self, record: OpportunityRecord, event: OpportunityEventRecord
    ) -> bool: ...

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None: ...

    def list_opportunities(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[OpportunityRecord, ...]: ...

    def count_opportunities(self, *, state: str | None = None) -> int: ...

    def list_opportunity_events(
        self, opportunity_id: str
    ) -> tuple[OpportunityEventRecord, ...]: ...

    # -- Triggers and risk verdicts --------------------------------------

    def save_trigger_event(self, record: TriggerEventRecord) -> bool: ...

    def update_trigger_event(self, record: TriggerEventRecord) -> bool: ...

    def get_trigger_event(self, trigger_id: str) -> TriggerEventRecord | None: ...

    def get_trigger_by_idempotency(self, idempotency_key: str) -> TriggerEventRecord | None: ...

    def list_trigger_events(
        self, *, opportunity_id: str | None = None, limit: int = 100
    ) -> tuple[TriggerEventRecord, ...]: ...

    def save_risk_verdict(self, record: RiskVerdictRecord) -> None: ...

    def get_risk_verdict(self, risk_verdict_id: str) -> RiskVerdictRecord | None: ...

    # -- Paper OMS -------------------------------------------------------

    def save_order_fill_position(
        self,
        *,
        order: OrderRecord,
        fill: FillRecord,
        position: PositionRecord,
        account: PaperAccountRecord,
        audit: AuditEventDraft | None = None,
    ) -> AuditEventRecord | None: ...

    def get_order(self, order_id: str) -> OrderRecord | None: ...

    def get_order_by_idempotency(self, idempotency_key: str) -> OrderRecord | None: ...

    def get_order_by_client_order_id(self, client_order_id: str) -> OrderRecord | None: ...

    def list_orders(self, *, limit: int = 100) -> tuple[OrderRecord, ...]: ...

    def list_fills(self, *, limit: int = 100) -> tuple[FillRecord, ...]: ...

    def get_fill_by_order_id(self, order_id: str) -> FillRecord | None: ...

    def list_positions(
        self, *, state: str | None = None, limit: int = 100
    ) -> tuple[PositionRecord, ...]: ...

    def get_position(self, position_id: str) -> PositionRecord | None: ...

    def get_paper_account(self, account_id: str) -> PaperAccountRecord | None: ...

    def save_paper_account(self, record: PaperAccountRecord) -> None: ...

    # -- Audit and system state ------------------------------------------

    def save_audit_event(self, record: AuditEventRecord) -> None: ...

    def append_audit_event(self, draft: AuditEventDraft) -> AuditEventRecord: ...

    def list_audit_events(self, *, limit: int = 100) -> tuple[AuditEventRecord, ...]: ...

    def last_audit_event(self) -> AuditEventRecord | None: ...

    def get_system_state(self, key: str) -> str | None: ...

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None: ...
