"""Store port: transactional state and the immutable-part manifest."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from oft.domain.data_quality import DataGap
from oft.domain.prompts import PromptRole, PromptStatus
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


class BudgetRecordConflictError(Exception):
    """The atomic call/budget write could not persist its budget ledger row.

    Raised inside the ``save_call_and_budget`` transaction when the budget
    insert resolves to a no-op (a planted or colliding ``record_id``), so the
    caller can never receive a phantom ``BudgetEntry`` that was not stored.
    """


class OmsConflictError(Exception):
    """A unique order constraint rejected the paper OMS insert.

    ``constraint`` names which invariant fired so the gateway can reconcile
    (``trigger_entry`` -> one entry per trigger, ``idempotency_key`` -> replay
    or conflict, ``client_order_id`` -> duplicate intent).
    """

    def __init__(self, constraint: str) -> None:
        super().__init__(f"order unique constraint violated: {constraint}")
        self.constraint = constraint
        self.message = f"order unique constraint violated: {constraint}"


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
    failed_stage: str | None = None
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
    system_prompt_id: str = ""
    system_prompt_version: int = 1
    system_prompt_hash: str = ""


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
class PromptTemplateRecord:
    """One prompt template family and its explicitly selected active version."""

    prompt_id: str
    role: PromptRole
    active_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PromptVersionRecord:
    """One immutable prompt version (user body plus its pinned system prompt)."""

    prompt_id: str
    version: int
    body: str
    body_hash: str
    is_active: bool
    created_at: str
    system_prompt_id: str = ""
    system_prompt_version: int = 1
    system_body: str = ""
    system_body_hash: str = ""
    required_variables_json: str = "[]"
    knowledge_artifact_hash: str | None = None
    status: PromptStatus = PromptStatus.DRAFT
    author: str = ""
    reason: str = ""
    is_default: bool = False


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
    period_week: str = ""
    period_month: str = ""
    role: str = ""


@dataclass(frozen=True)
class BudgetSettingsRecord:
    """Operator overrides for the ADR-0005 budget thresholds (one row)."""

    daily_usd: str
    weekly_usd: str
    monthly_usd: str
    warn_pct: float
    updated_at: str
    id: str = "default"


@dataclass(frozen=True)
class ProviderCapabilityRecord:
    """The last recorded capability probe for one provider."""

    provider: str
    model: str
    strict_schema: bool
    degraded: bool
    max_output_tokens: int
    status: str
    error_code: str | None
    notes: str
    latency_ms: int
    checked_at: str


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
class ConditionRecord:
    """The persisted mirror of one contract condition (queryable)."""

    condition_id: str
    opportunity_id: str
    kind: str
    seq: int
    type: str
    params_json: str
    window_start: str
    window_end: str
    required_data_json: str
    evaluable: bool
    unavailable_reason: str | None = None
    last_value: str | None = None
    last_evaluated_at: str | None = None
    last_result: bool | None = None


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
    snapshot_hash: str = ""
    as_of: str = ""
    data_quality: str = ""
    condition_count: int = 0


@dataclass(frozen=True)
class RiskVerdictRecord:
    """A persisted deterministic risk verdict.

    The measured gate inputs are stored alongside the contract-valid payload so
    the gateway can re-derive ``inputs_hash`` without re-measuring them (D1).
    """

    risk_verdict_id: str
    trigger_id: str
    decision_id: str
    verdict: str
    binding_cap: str
    computed_at: str
    expires_at: str
    payload_json: str
    spread_bps: float | None = None
    expected_slippage_bps: float | None = None
    daily_loss_usd: str | None = None


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
    trigger_id: str | None = None
    decision_id: str | None = None
    purpose: str = "ENTRY"


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
    # Optimistic-concurrency guard: when set, the in-place OPEN update must
    # still match this exact previous payload (a lost-update guard).
    expected_payload_json: str | None = None


@dataclass(frozen=True)
class PaperAccountRecord:
    account_id: str
    environment: str
    currency: str
    initial_balance: str
    balance: str
    updated_at: str


@dataclass(frozen=True)
class ConfigRecord:
    """One persisted runtime setting row (``config`` table).

    ``value_json`` is the JSON-encoded setting value, so the column can hold a
    string, number, boolean, or object without a per-setting schema change.
    """

    key: str
    value_json: str
    updated_at: str
    updated_by: str


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


@dataclass(frozen=True)
class OmsCommit:
    """The account row and audit event written with one paper fill."""

    account: PaperAccountRecord
    audit: AuditEventRecord | None


@dataclass(frozen=True)
class PositionEventRecord:
    """One append-only entry in a position's management timeline.

    ``seq`` is ``None`` for a draft: the store assigns the next contiguous
    sequence inside the close transaction. An explicit positive value is kept
    as-is for tests and replay fixtures.
    """

    position_id: str
    at: str
    action: str
    order_id: str | None
    note: str
    seq: int | None = None


@dataclass(frozen=True)
class TradeLogRecord:
    """The one closed-trade record for a position, plus its query keys.

    The scalar columns duplicate fields of ``payload_json``; the broker builds
    them from the rendered payload so the two can never disagree.
    """

    trade_id: str
    position_id: str
    account_id: str
    environment: str
    symbol: str
    side: str
    opened_at: str
    closed_at: str
    net_pnl_usd: str
    idempotency_key: str
    payload_json: str
    order_id: str = ""
    fill_id: str = ""
    risk_verdict_id: str = ""
    decision_id: str = ""
    # The advisory ``REVIEW`` decision linked to this trade, if reviewed.
    review_decision_id: str | None = None


@dataclass(frozen=True)
class ClosedPositionRecord:
    """Everything one full close commits in a single transaction."""

    order: OrderRecord
    fill: FillRecord
    position: PositionRecord
    account: PaperAccountRecord
    balance_delta: str
    event: PositionEventRecord
    trade_log: TradeLogRecord
    audits: tuple[AuditEventDraft, ...] = ()


@dataclass(frozen=True)
class PositionAdjustmentRecord:
    """Everything one partial close or add commits in a single transaction.

    ``position`` carries the already-updated OPEN payload; the store updates the
    one OPEN row in place. No trade log is written for a partial close or add.
    """

    order: OrderRecord
    fill: FillRecord
    position: PositionRecord
    account: PaperAccountRecord
    balance_delta: str
    event: PositionEventRecord
    audits: tuple[AuditEventDraft, ...] = ()


@dataclass(frozen=True)
class ManagementUpdateRecord:
    """A management action that changes the OPEN position without an order.

    Used by ``HOLD`` (which appends a management-history entry) and by a stop
    or target update (which changes only those fields). The update and its audit
    events commit in one transaction, so a recorded action can never exist
    without its audit trail.
    """

    position: PositionRecord
    audits: tuple[AuditEventDraft, ...]


@dataclass(frozen=True)
class FundingAccrualRecord:
    """One recorded funding settlement applied to a position.

    ``amount_usd`` is the signed cash flow (a LONG paying a positive rate is
    negative). ``side`` and ``size`` are the position state the amount was
    priced on, so the accrual can be re-derived without replaying the timeline.
    The ``(position_id, settlement_time)`` unique constraint makes a replay or
    restart a no-op.
    """

    accrual_id: str
    position_id: str
    settlement_time: str
    rate: str
    mark_price: str
    amount_usd: str
    created_at: str
    side: str
    size: str


@dataclass(frozen=True)
class FundingAccrualCommit:
    """Everything one funding accrual commits in a single transaction.

    The balance delta is not carried separately: the store derives it from
    ``accrual.amount_usd`` and verifies the position's funding total advanced by
    exactly that amount before committing.
    """

    accrual: FundingAccrualRecord
    position: PositionRecord
    account: PaperAccountRecord
    event: PositionEventRecord
    audits: tuple[AuditEventDraft, ...] = ()


@dataclass(frozen=True)
class CloseCommitResult:
    """The committed account row and audit records from one close."""

    account: PaperAccountRecord
    trade_log: TradeLogRecord
    audits: tuple[AuditEventRecord, ...] = ()


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

    def list_reports_for_symbol(
        self, symbol: str, *, since: str | None = None, limit: int | None = None
    ) -> tuple[ReportRecord, ...]:
        """Recorded reports for one symbol, oldest generated first.

        Used by funding accrual: each recorded report contributes the funding
        settlements it observed, so a later report can record a settlement that
        did not exist when the position's entry report was frozen. ``since``
        bounds the read to the newest reports generated at/after the position's
        opening plus the single newest report before it; ``limit`` caps the
        post-entry rows.
        """
        ...

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

    def expire_job_leases(self, *, job_type: str, now: str) -> int: ...

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

    def get_decisions(self, decision_ids: Sequence[str]) -> dict[str, DecisionRecord]:
        """Read several decisions in one parameterized query, keyed by id.

        Missing ids are simply absent from the mapping; this is the batched
        read used by list projections so one page does not issue N point reads.
        """
        ...

    def list_decisions(self, *, role: str | None = None) -> tuple[DecisionRecord, ...]: ...

    # -- Prompt templates and immutable versions (ADR-0009) --------------

    def save_prompt_template(self, record: PromptTemplateRecord) -> None: ...

    def get_prompt_template(self, prompt_id: str) -> PromptTemplateRecord | None: ...

    def list_prompt_templates(self) -> tuple[PromptTemplateRecord, ...]: ...

    def save_prompt_version(self, record: PromptVersionRecord) -> None: ...

    def get_prompt_version(self, prompt_id: str, version: int) -> PromptVersionRecord | None: ...

    def list_prompt_versions(self, prompt_id: str) -> tuple[PromptVersionRecord, ...]: ...

    def activate_prompt_version(
        self, prompt_id: str, version: int, *, activated_at: str
    ) -> bool: ...

    # -- Budget ledger ---------------------------------------------------

    def save_call_and_budget(
        self,
        call: LLMCallRecord,
        budget: BudgetRecord,
        compute: Callable[[dict[str, dict[str, Any]]], tuple[str, list[AuditEventDraft]]],
    ) -> tuple[LLMCallRecord, BudgetRecord]:
        """Insert one call row, one budget row, and any threshold audits atomically.

        Runs under one ``BEGIN IMMEDIATE`` transaction so a concurrent or
        cross-process writer cannot miss a crossing or split the rows. The store
        allocates a unique call id (suffix ``-<8 hex>`` to both rows) when the
        base id already exists. ``compute`` receives the in-transaction prior
        per-period totals and returns ``(budget_state, audit_drafts)``; a call
        that crosses several periods yields one draft per crossed period.
        """
        ...

    def save_budget_record(self, record: BudgetRecord) -> bool: ...

    def list_budget_records(
        self, *, decision_id: str | None = None
    ) -> tuple[BudgetRecord, ...]: ...

    def last_budget_record(self) -> BudgetRecord | None: ...

    def list_budget_records_for_period(self, period: str, key: str) -> tuple[BudgetRecord, ...]: ...

    def budget_totals(self, *, day: str, week: str, month: str) -> dict[str, dict[str, Any]]: ...

    def get_budget_settings(self) -> BudgetSettingsRecord | None: ...

    def save_budget_settings(self, record: BudgetSettingsRecord) -> None: ...

    # -- Provider capability probes --------------------------------------

    def save_provider_capability(self, record: ProviderCapabilityRecord) -> None: ...

    def get_provider_capability(self, provider: str) -> ProviderCapabilityRecord | None: ...

    def list_provider_capabilities(self) -> tuple[ProviderCapabilityRecord, ...]: ...

    # -- Opportunity ledger ----------------------------------------------

    def save_opportunity(
        self,
        record: OpportunityRecord,
        event: OpportunityEventRecord | None = None,
        conditions: Sequence[ConditionRecord] = (),
    ) -> bool:
        """Persist a new opportunity, event, and condition mirror atomically.

        Returns ``True`` when the row was inserted. A duplicate create is an
        idempotent no-op that leaves the payload and mirror untouched and
        returns ``False``; existing opportunities change only via
        :meth:`transition_opportunity`.
        """
        ...

    def transition_opportunity(
        self, record: OpportunityRecord, event: OpportunityEventRecord
    ) -> bool: ...

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None: ...

    def list_opportunities(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[OpportunityRecord, ...]: ...

    def list_due_opportunities(
        self, now: str, *, limit: int = 1000
    ) -> tuple[OpportunityRecord, ...]: ...

    def count_opportunities(self, *, state: str | None = None) -> int: ...

    def list_opportunity_events(
        self, opportunity_id: str
    ) -> tuple[OpportunityEventRecord, ...]: ...

    def list_conditions(
        self, opportunity_id: str, kind: str | None = None
    ) -> tuple[ConditionRecord, ...]: ...

    def update_condition_evaluation(
        self,
        opportunity_id: str,
        condition_id: str,
        *,
        last_value: str | None,
        last_evaluated_at: str | None,
        last_result: bool | None,
    ) -> bool: ...

    # -- Triggers and risk verdicts --------------------------------------

    def save_trigger_event(self, record: TriggerEventRecord) -> bool: ...

    def save_trigger_and_transition(
        self,
        record: TriggerEventRecord,
        opportunity: OpportunityRecord,
        event: OpportunityEventRecord,
    ) -> Literal["inserted", "existing", "conflict"]:
        """Insert a trigger and move its opportunity to TRIGGERED atomically.

        One ``BEGIN IMMEDIATE`` transaction inserts the trigger
        (``ON CONFLICT(idempotency_key) DO NOTHING``) and, when the insert
        happened, applies the ``OPEN -> TRIGGERED`` guard plus the history
        event. A failed guard rolls the trigger insert back and returns
        ``conflict``. When the trigger already exists, an opportunity that is
        still ``OPEN`` is transitioned idempotently in the same transaction
        (``existing``); an already ``TRIGGERED`` opportunity is ``existing``;
        any other state is ``conflict``.
        """
        ...

    def update_trigger_event(
        self,
        record: TriggerEventRecord,
        *,
        expected_status: str | None = None,
        require_no_order: bool = False,
        require_no_entry_order: bool = False,
    ) -> bool:
        """Conditionally write a trigger payload (compare-and-set).

        ``expected_status`` restricts the update to the row currently in that
        state, ``require_no_order`` requires the stored payload to carry no
        ``created_order_id``, and ``require_no_entry_order`` refuses the write
        when an ENTRY order already exists for the trigger. Returns ``False``
        (zero rows) when the precondition no longer holds; callers must surface
        a typed conflict instead of overwriting a concurrent transition.
        """
        ...

    def get_trigger_event(self, trigger_id: str) -> TriggerEventRecord | None: ...

    def get_trigger_by_idempotency(self, idempotency_key: str) -> TriggerEventRecord | None: ...

    def list_trigger_events(
        self,
        *,
        opportunity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[TriggerEventRecord, ...]: ...

    def count_trigger_events(
        self, *, opportunity_id: str | None = None, status: str | None = None
    ) -> int: ...

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
        balance_delta: str = "0",
        audit: AuditEventDraft | None = None,
    ) -> OmsCommit: ...

    def get_order(self, order_id: str) -> OrderRecord | None: ...

    def get_order_by_idempotency(self, idempotency_key: str) -> OrderRecord | None: ...

    def get_order_by_client_order_id(self, client_order_id: str) -> OrderRecord | None: ...

    def get_entry_order_for_trigger(self, trigger_id: str) -> OrderRecord | None:
        """The one entry order bound to a trigger, if any (D6)."""
        ...

    def list_orders(self, *, limit: int = 100, offset: int = 0) -> tuple[OrderRecord, ...]: ...

    def count_orders(self) -> int: ...

    def list_fills(self, *, limit: int = 100, offset: int = 0) -> tuple[FillRecord, ...]: ...

    def count_fills(self) -> int: ...

    def get_fill_by_order_id(self, order_id: str) -> FillRecord | None: ...

    def list_positions(
        self, *, state: str | None = None, limit: int | None = 100, offset: int = 0
    ) -> tuple[PositionRecord, ...]: ...

    def count_positions(self, *, state: str | None = None, symbol: str | None = None) -> int: ...

    def get_position(self, position_id: str) -> PositionRecord | None: ...

    def update_open_position_payload(
        self,
        position_id: str,
        *,
        payload_json: str,
        expected_payload_json: str | None = None,
    ) -> bool:
        """Replace an OPEN position's payload; ``False`` when the guard misses.

        The caller owns the payload (including its version/``updated_at`` bump).
        Only ``state = 'OPEN'`` rows change, so a concurrent close can never be
        overwritten and no order is created for a stale tick. When
        ``expected_payload_json`` is supplied the update is optimistic and also
        requires the row to still carry that exact payload, so a concurrent
        funding accrual or adjustment is never silently lost.
        """
        ...

    def save_close(self, record: ClosedPositionRecord) -> CloseCommitResult:
        """Atomically commit a full close.

        Writes the account balance delta, order, fill, the in-place position
        update, the position event, the trade log, and every audit draft in one
        ``BEGIN IMMEDIATE`` transaction. Any error rolls the whole unit back;
        the position update must match exactly one OPEN row.
        """
        ...

    def save_position_adjustment(self, record: PositionAdjustmentRecord) -> OmsCommit:
        """Atomically commit a partial close or an add.

        Writes the account balance delta, order, fill, the in-place OPEN
        position update, the position event, and the audits in one
        ``BEGIN IMMEDIATE`` transaction. A zero-row position update aborts the
        whole unit; no trade log is written.
        """
        ...

    def save_management_update(self, record: ManagementUpdateRecord) -> bool:
        """Atomically replace an OPEN position payload and append its audits.

        Used by ``HOLD`` and by a stop/target update, which write no order or
        fill. The optimistic payload guard aborts the transaction when the
        position changed concurrently (``OmsConflictError``); every audit draft
        commits with the position update.
        """
        ...

    def save_funding_accrual(self, record: FundingAccrualCommit) -> bool:
        """Atomically commit one funding settlement.

        Inserts the accrual with ``ON CONFLICT(position_id, settlement_time) DO
        NOTHING``; when it was already recorded the unit is rolled back and
        ``False`` is returned, so a restart or replay cannot double-apply the
        amount. Otherwise the account delta, updated OPEN position, event, and
        audits commit together and ``True`` is returned.
        """
        ...

    def list_funding_accruals(self, position_id: str) -> tuple[FundingAccrualRecord, ...]: ...

    def get_trade_log(self, trade_id: str) -> TradeLogRecord | None: ...

    def link_trade_log_review(self, trade_id: str, *, decision_id: str) -> bool:
        """Atomically record the advisory review decision on a trade log.

        Sets the ``review_decision_id`` column and the matching payload field in
        one transaction, leaving every other payload field and metric untouched.
        Returns ``False`` when the trade row does not exist; an unreadable
        payload is reported by raising :class:`ValueError` so a caller can
        surface a typed ``DATA_UNAVAILABLE`` rather than overwrite it.
        """
        ...

    def get_trade_log_for_position(self, position_id: str) -> TradeLogRecord | None: ...

    def list_trade_logs(
        self, *, symbol: str | None = None, limit: int | None = 100, offset: int = 0
    ) -> tuple[TradeLogRecord, ...]: ...

    def count_trade_logs(self, *, symbol: str | None = None) -> int: ...

    def list_position_events(self, position_id: str) -> tuple[PositionEventRecord, ...]: ...

    def get_paper_account(self, account_id: str) -> PaperAccountRecord | None: ...

    def save_paper_account(self, record: PaperAccountRecord) -> None: ...

    def seed_paper_account(self, record: PaperAccountRecord) -> PaperAccountRecord:
        """Insert the seed row once; never overwrite a live balance (D3)."""
        ...

    # -- Audit and system state ------------------------------------------

    def append_audit_event(self, draft: AuditEventDraft) -> AuditEventRecord: ...

    def list_audit_events(self, *, limit: int = 100) -> tuple[AuditEventRecord, ...]: ...

    def list_audit_events_ascending(
        self, *, limit: int | None = None
    ) -> tuple[AuditEventRecord, ...]:
        """Every event in chain order, for tamper verification (D7)."""
        ...

    def last_audit_event(self) -> AuditEventRecord | None: ...

    def last_audit_event_of_type(self, event_type: str) -> AuditEventRecord | None:
        """The newest chained event of one type, or ``None``.

        Schema v27 indexes ``audit_events(event_type, seq DESC)``, so this is a
        bounded index seek even when no event of that type has ever been
        written. Callers on hot metadata paths may still prefer
        ``get_system_state_updated_at`` (which reads the ``system_state`` row).
        """
        ...

    def audit_store_initialized(self) -> bool:
        """Whether the database exists and carries the ``audit_events`` table (D7)."""
        ...

    def get_system_state(self, key: str) -> str | None: ...

    def get_system_state_updated_at(self, key: str) -> str | None:
        """The ``updated_at`` of one ``system_state`` row, or ``None``.

        A single indexed primary-key lookup, used by metadata-only reads that
        need a last-change timestamp without scanning the audit chain.
        """
        ...

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None: ...

    def save_risk_caps(
        self, *, value: str, updated_at: str, audit: AuditEventDraft
    ) -> AuditEventRecord:
        """Persist the caps document and its ``RISK_CAPS_UPDATED`` audit atomically."""
        ...

    def save_risk_caps_change(
        self,
        build: Callable[[str | None], tuple[str, AuditEventDraft] | None],
    ) -> tuple[str | None, AuditEventRecord | None]:
        """Read-modify-write the ``system_state.risk_caps`` document atomically.

        The store reads the current value inside one ``BEGIN IMMEDIATE``
        transaction, calls the pure ``build`` callback with it, and upserts the
        returned document plus its audit draft (or does nothing when ``build``
        returns ``None``). ``before`` values derived inside the callback are
        therefore accurate under concurrency, and a validation failure raised by
        the callback rolls the transaction back with no partial write. Returns
        the committed value (or the existing value when nothing was written) and
        the committed audit record, if any.
        """
        ...

    # -- Persisted runtime settings --------------------------------------

    def get_config(self, key: str) -> ConfigRecord | None: ...

    def list_config(self) -> tuple[ConfigRecord, ...]: ...

    def set_config(self, record: ConfigRecord) -> None: ...

    def save_config_with_audit(
        self, record: ConfigRecord, audit: AuditEventDraft
    ) -> AuditEventRecord:
        """Upsert one setting row and append its audit event in one transaction."""
        ...

    def save_config_change(
        self,
        key: str,
        build: Callable[[ConfigRecord | None], tuple[ConfigRecord, AuditEventDraft] | None],
    ) -> tuple[ConfigRecord | None, AuditEventRecord | None]:
        """Read-modify-write one setting atomically.

        The store reads the row inside one ``BEGIN IMMEDIATE`` transaction,
        calls the pure ``build`` callback with it, and upserts the returned
        record plus its audit draft (or does nothing when ``build`` returns
        ``None``). ``before`` values derived inside the callback are therefore
        accurate under concurrency. Returns the committed row (or the existing
        row when nothing was written) and the committed audit record, if any.
        """
        ...
