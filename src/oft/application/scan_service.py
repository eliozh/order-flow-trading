"""One scan: ``DATA -> METRICS -> LLM (optional) -> REPORT``.

The service is the single writer for the read path introduced by slice 08. It
resamples the persisted ``agg_trades`` window into UTC bars, computes the
versioned order-flow document, freezes a ``market-snapshot`` that embeds that
document, and stores a CALC report. When a provider is configured the optional
LLM stage persists ``llm_calls``/decisions and embeds the narrative and LLM
state in the report; without a provider the report stays CALC-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from oft.adapters.parquet_store import read_liquidations
from oft.application.analysis_service import AnalysisService
from oft.application.freshness import check_live_source
from oft.application.llm_orchestrator import LLMRuntimeRegistry, provider_configured
from oft.application.metric_service import MetricInputs, MetricRequest, MetricService
from oft.application.resampling_service import BarResampleRequest, ResamplingService
from oft.config import Settings
from oft.domain.metrics.footprint import (
    DEFAULT_IMBALANCE_THRESHOLD,
    DEFAULT_STACKED_MIN_LEVELS,
    DEFAULT_TOP_IMBALANCES,
)
from oft.domain.metrics.liquidations import (
    LiquidationEvent,
    compute_liquidations,
)
from oft.domain.metrics.types import decimal_str
from oft.domain.metrics.version import MetricVersion, orderflow_metric_version
from oft.domain.resampling import SourceMode
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.llm import LLMPort
from oft.ports.store import ReportRecord, ScanRecord, StorePort, StoreUnavailableError

logger = structlog.get_logger(__name__)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
SUPPORTED_SOURCES = frozenset({"backfill", "live"})
SCAN_STAGES = ("QUEUED", "DATA", "METRICS", "LLM", "REPORT", "DONE")
TERMINAL_STAGES = frozenset({"DONE", "DEGRADED", "FAILED"})
WINDOW_LOOKBACK_DAYS = 7
SNAPSHOT_SCHEMA_VERSION = "1.0.0"
LLM_DISABLED_REASON = (
    "LLM not configured. This build produces metrics-only CALC reports; "
    "no narrative, decision, or LLM call was generated."
)
LLM_PROVIDER_NOT_CONFIGURED_REASON = (
    "LLM_PROVIDER_NOT_CONFIGURED: no provider is configured, so this report is "
    "CALC-only. Configure a provider to enable narrative analysis."
)


class ScanError(Exception):
    """Base typed scan failure; ``code`` matches ``error-catalog.json``."""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ScanValidationError(ScanError):
    code = "VALIDATION_FAILED"


class ScanAlreadyRunning(ScanError):
    code = "SCAN_ALREADY_RUNNING"

    def __init__(
        self,
        message: str = "A scan is already running.",
        *,
        running_scan_id: str | None = None,
    ) -> None:
        if running_scan_id:
            message = f"{message} Running scan: {running_scan_id}"
        super().__init__(message)
        self.running_scan_id = running_scan_id


class ScanDataUnavailable(ScanError):
    code = "DATA_UNAVAILABLE"


class ScanMetricsIncomplete(ScanError):
    code = "METRICS_INCOMPLETE"


class ScanContainer(Protocol):
    @property
    def settings(self) -> Settings: ...

    @property
    def clock(self) -> ClockPort: ...

    @property
    def store(self) -> StorePort: ...

    @property
    def parquet(self) -> Any: ...

    @property
    def llm_provider(self) -> LLMPort | None: ...

    @property
    def llm_runtime(self) -> LLMRuntimeRegistry | None: ...

    def provider_for(self, requested: str | None = None) -> LLMPort | None: ...

    def refresh_llm_settings(self) -> Settings: ...


@dataclass(frozen=True)
class ScanRequest:
    """Inputs for one manual scan (CALC-only or stub-LLM enabled)."""

    symbol: str
    period: str = "4h"
    source: str = "backfill"
    metrics_only: bool = False
    start: date | None = None
    end: date | None = None
    trigger_source: str = "MANUAL"
    provider: str | None = None
    batch_id: str | None = None

    def __post_init__(self) -> None:
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ScanValidationError("symbol must match ^[A-Z0-9]{2,24}$")
        if self.source not in SUPPORTED_SOURCES:
            raise ScanValidationError(
                f"unsupported source {self.source!r}; supported: {sorted(SUPPORTED_SOURCES)}"
            )
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ScanValidationError("end must be on or after start")


@dataclass(frozen=True)
class ScanRun:
    """A completed scan and the CALC/LLM report it produced."""

    scan: ScanRecord
    report: ReportRecord
    llm_state: dict[str, Any] = field(default_factory=dict)
    symbol_stage: str = "DONE"
    llm_tokens: int = 0
    cost_usd: str = "0"
    cost_unpriced: bool = False


def _window_from_manifest(
    store: StorePort,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[datetime, datetime] | None:
    days = sorted(
        {
            entry.date
            for entry in store.list_manifest(symbol=symbol)
            if entry.data_type == "agg_trades" and entry.date
        }
    )
    if not days:
        return None
    first_day = (
        max(date.fromisoformat(days[0]), start)
        if start is not None
        else date.fromisoformat(days[0])
    )
    last_day = (
        min(date.fromisoformat(days[-1]), end) if end is not None else date.fromisoformat(days[-1])
    )
    if last_day < first_day:
        return None
    return (
        datetime.combine(first_day, datetime.min.time(), tzinfo=UTC),
        datetime.combine(last_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
    )


def _data_unavailable_message(store: StorePort, request: ScanRequest) -> str:
    """Explain a missing data window with the exact recorded gap when known."""
    gaps = store.list_data_gaps(symbol=request.symbol, data_type="agg_trades")
    if gaps:
        gap = gaps[0]
        return (
            f"Data gap {gap.gap_id}: {gap.start} -> {gap.end} ({gap.reason}); "
            f"no agg_trades are available for {request.symbol}."
        )
    if request.start is not None or request.end is not None:
        return (
            f"No published agg_trades exist for {request.symbol} in the requested window "
            f"{request.start or '...'} -> {request.end or '...'}; complete a backfill first."
        )
    return f"No published agg_trades exist for {request.symbol}; complete a backfill first."


def _metric_version(request: MetricRequest) -> MetricVersion:
    return orderflow_metric_version(
        period=request.period,
        unit=request.unit,
        tick_size=request.tick_size,
        value_area_pct=request.value_area_pct,
        bin_size_multiple=request.bin_size_multiple,
        footprint_imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
        footprint_stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
        footprint_top_imbalances=DEFAULT_TOP_IMBALANCES,
    )


def measured_liquidation_inputs(container: ScanContainer, request: MetricRequest) -> MetricInputs:
    """Read persisted liquidations for this symbol/version; never invent history."""
    version = _metric_version(request)
    try:
        rows = read_liquidations(
            container.settings,
            symbol=request.symbol,
            metric_version=version.identifier,
            store=container.store,
        )
    except StoreUnavailableError as exc:
        logger.warning("liquidations_unreadable", symbol=request.symbol, error=str(exc))
        return MetricInputs(liquidation_unavailable=True)
    if not rows:
        return MetricInputs(
            liquidations=compute_liquidations((), symbol=request.symbol, coverage_start=None)
        )
    events: list[LiquidationEvent] = []
    for row in rows:
        events.append(
            LiquidationEvent(
                received_at=cast(datetime, row["received_at"]),
                trade_time=cast(datetime, row["trade_time"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),  # type: ignore[arg-type]
                price=Decimal(str(row["price"])),
                average_price=Decimal(str(row["average_price"])),
                quantity=Decimal(str(row["original_quantity"])),
                order_type=str(row["order_type"]),
                time_in_force=str(row["time_in_force"]),
                order_status=str(row["order_status"]),
                last_fill_quantity=(
                    Decimal(str(row["last_fill_quantity"]))
                    if row.get("last_fill_quantity") is not None
                    else None
                ),
                source_seq=(
                    int(str(row["source_seq"])) if row.get("source_seq") is not None else None
                ),
            )
        )
    coverage_start = min(event.received_at for event in events)
    return MetricInputs(
        liquidations=compute_liquidations(
            events, symbol=request.symbol, coverage_start=coverage_start
        )
    )


def _open_interest_at(points: tuple[Any, ...], bar_end: datetime) -> Decimal | None:
    selected: Decimal | None = None
    for point in points:
        if point.ts <= bar_end:
            selected = point.open_interest
        else:
            break
    return selected


def _snapshot_bars(
    resample_bars: tuple[Any, ...],
    document: Any,
) -> list[dict[str, object]]:
    points = {point.bar_start: point for point in document.cvd.points}
    oi_points = tuple(document.open_interest.points) if document.open_interest else ()
    funding_points = (
        sorted(document.funding.history, key=lambda point: point.ts)
        if document.funding is not None
        else []
    )

    def funding_at(bar_end: datetime) -> Decimal | None:
        selected: Decimal | None = None
        for point in funding_points:
            if point.ts <= bar_end:
                selected = point.rate
            else:
                break
        return selected

    bars: list[dict[str, object]] = []
    for bar in resample_bars:
        point = points.get(bar.bar_start)
        if point is None:
            raise ScanMetricsIncomplete(
                f"bars and metrics disagree: no CVD point for {utc_timestamp(bar.bar_start)}"
            )
        total = point.buy_volume + point.sell_volume
        if total != bar.volume or point.buy_volume != bar.taker_buy_volume:
            raise ScanMetricsIncomplete(
                f"bars and metrics disagree at {utc_timestamp(bar.bar_start)}"
            )
        open_interest = _open_interest_at(oi_points, bar.bar_end)
        funding_rate = funding_at(bar.bar_end)
        bars.append(
            {
                "bar_start": utc_timestamp(bar.bar_start),
                "bar_end": utc_timestamp(bar.bar_end),
                "open": decimal_str(bar.open),
                "high": decimal_str(bar.high),
                "low": decimal_str(bar.low),
                "close": decimal_str(bar.close),
                "volume": decimal_str(bar.volume),
                "quote_volume": decimal_str(bar.quote_volume),
                "trade_count": bar.trade_count,
                "taker_buy_volume": decimal_str(bar.taker_buy_volume),
                "delta": decimal_str(point.delta),
                "cvd": decimal_str(point.cvd),
                "open_interest": None if open_interest is None else decimal_str(open_interest),
                "funding_rate": None if funding_rate is None else decimal_str(funding_rate),
            }
        )
    return bars


def _snapshot_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_market_snapshot(
    *,
    document: Any,
    bars: list[dict[str, object]],
    contract: Any,
    snapshot_id: str,
    source_mode: str = "BACKFILL",
) -> dict[str, object]:
    """Freeze the bounded, code-built snapshot that embeds the metric document."""
    metrics_payload = json.loads(document.json_text)
    if not bars:
        raise ScanDataUnavailable("cannot build a snapshot without closed bars")
    last_close = str(bars[-1]["close"])
    quality_reasons = list(metrics_payload.get("quality_reasons", []))
    measured_funding = (
        document.funding is not None
        and document.funding.current_rate is not None
        and document.funding.mark_price > 0
    )
    if measured_funding:
        mark_price = decimal_str(document.funding.mark_price)
    else:
        mark_price = last_close
        quality_reasons.append("mark price not measured; snapshot mark uses the last trade close")
    period = str(metrics_payload["period"])
    contract_specs = {
        "contract_type": contract.contract_type,
        "underlying_type": contract.underlying_type or "UNKNOWN",
        "tick_size": decimal_str(contract.tick_size),
        "step_size": decimal_str(contract.step_size),
        "min_notional": decimal_str(contract.min_notional),
        "price_precision": contract.price_precision,
        "quantity_precision": contract.quantity_precision,
        "onboard_date": utc_timestamp(datetime.fromtimestamp(contract.onboard_date / 1000, tz=UTC)),
    }
    snapshot: dict[str, object] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "symbol": metrics_payload["symbol"],
        "timeframe": period,
        "as_of": metrics_payload["computed_at"],
        "window": metrics_payload["window"],
        "source_mode": source_mode,
        "data_quality": metrics_payload["data_quality"],
        "quality_reasons": quality_reasons,
        "last_price": last_close,
        "mark_price": mark_price,
        "bars": bars,
        "orderflow": metrics_payload,
        "session": {
            "underlying_session": "UNKNOWN",
            "is_no_trading": False,
            "note": "Session state is not measured in this metrics-only slice.",
        },
        "contract_specs": contract_specs,
        "sources": metrics_payload.get("sources", []),
        "gaps": metrics_payload.get("gaps", []),
    }
    snapshot["snapshot_hash"] = _snapshot_hash(snapshot)
    return snapshot


def _scan_id(now: datetime) -> str:
    return f"SCAN-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _report_id(symbol: str, now: datetime) -> str:
    # Scans can complete within the same second; the report id is a primary
    # key and must stay unique even under rapid sequential or concurrent runs.
    return f"RPT-{symbol}-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8].upper()}"


def _snapshot_id(
    symbol: str,
    now: datetime,
    *,
    window_start: str = "",
    window_end: str = "",
) -> str:
    """Deterministic snapshot id: the same window and clock always match."""
    digest = (
        hashlib.sha256(f"{symbol}:{now:%Y%m%d%H%M%S}:{window_start}:{window_end}".encode())
        .hexdigest()[:8]
        .upper()
    )
    return f"SNAP-{symbol}-{now:%Y%m%d%H%M%S}-{digest}"


def _summary_values(document: Any) -> dict[str, str]:
    summary = document.cvd.summary
    return {
        "buy_volume": decimal_str(summary.buy_volume),
        "sell_volume": decimal_str(summary.sell_volume),
        "delta": decimal_str(summary.delta),
        "cvd": decimal_str(document.cvd.points[-1].cvd) if document.cvd.points else "0",
    }


def _window_policy(
    *,
    period: str,
    window_start: datetime,
    window_end: datetime,
    requested: bool,
) -> dict[str, object]:
    """Record the main/period window and the 3-7 day structure lookback."""
    lookback_start = window_start - timedelta(days=WINDOW_LOOKBACK_DAYS)
    return {
        "main_window": {
            "start": utc_timestamp(window_start),
            "end": utc_timestamp(window_end),
            "period": period,
        },
        "main_window_source": "REQUESTED" if requested else "MANIFEST",
        "lookback_days": WINDOW_LOOKBACK_DAYS,
        "lookback_window": {
            "start": utc_timestamp(lookback_start),
            "end": utc_timestamp(window_end),
        },
        "note": (
            "The main window matches the scan period; volume profile and market "
            "structure use up to a 7-day lookback."
        ),
    }


def _llm_totals(llm_state: Mapping[str, object]) -> tuple[int, str, bool]:
    """Return ``(tokens, cost_usd, unpriced)`` from one report's LLM band."""
    if not bool(llm_state.get("configured")):
        return 0, "0", False
    usage = llm_state.get("usage")
    tokens = 0
    if isinstance(usage, Mapping):
        value = usage.get("total_tokens")
        if isinstance(value, int):
            tokens = value
    cost = llm_state.get("cost")
    amount = "0"
    unpriced = True
    if isinstance(cost, Mapping):
        raw_amount = cost.get("amount_usd")
        if raw_amount is not None:
            amount = str(raw_amount)
        unpriced = bool(cost.get("unpriced", True))
    return tokens, amount, unpriced


def scan_symbol_stage(llm_state: Mapping[str, object]) -> str:
    """A symbol is DEGRADED only when a configured LLM call was rejected."""
    if not bool(llm_state.get("configured")):
        return "DONE"
    status = llm_state.get("validation_status")
    return "DONE" if status in {"VALID", "REPAIRED"} else "DEGRADED"


def scan_llm_stage(llm_state: Mapping[str, object]) -> str:
    """The LLM cell of a report: ``SKIPPED`` when no provider ran."""
    if not bool(llm_state.get("configured")):
        return "SKIPPED"
    status = llm_state.get("validation_status")
    if status in {"VALID", "REPAIRED"}:
        return "DONE"
    if status == "REJECTED":
        return "FAILED"
    return "SKIPPED"


def scan_llm_status(llm_state: Mapping[str, object]) -> str | None:
    if not bool(llm_state.get("configured")):
        return None
    status = llm_state.get("validation_status")
    return str(status) if status is not None else None


class ScanService:
    """Run one scan serially under the daemon scan lock."""

    def __init__(
        self,
        *,
        container: ScanContainer,
        lock: threading.Lock | None = None,
        inputs: MetricInputs | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> None:
        self._container = container
        self._lock = lock or threading.Lock()
        self._inputs = inputs
        self._on_stage = on_stage

    def _stage(self, name: str) -> None:
        if self._on_stage is not None:
            self._on_stage(name)

    def run(self, request: ScanRequest, *, scan_id: str | None = None) -> ScanRun:
        if not self._lock.acquire(blocking=False):
            raise ScanAlreadyRunning()
        try:
            return self._run_locked(request, scan_id=scan_id)
        finally:
            self._lock.release()

    def _run_locked(self, request: ScanRequest, *, scan_id: str | None = None) -> ScanRun:
        container = self._container
        store = container.store
        clock = container.clock
        started = clock.now()
        store.fail_stale_scans(now=utc_timestamp(started))
        window = _window_from_manifest(store, request.symbol, request.start, request.end)
        if window is None:
            raise ScanDataUnavailable(_data_unavailable_message(store, request))
        window_start, window_end = window
        contract = store.get_symbol(request.symbol)
        if contract is None:
            raise ScanValidationError(f"Symbol {request.symbol} is not in the symbol registry.")
        tick_size = contract.tick_size
        source_mode: SourceMode = "LIVE" if request.source == "live" else "BACKFILL"
        resolved_scan_id = scan_id or _scan_id(started)
        scan = ScanRecord(
            scan_id=resolved_scan_id,
            symbol=request.symbol,
            period=request.period,
            status="RUNNING",
            trigger_source=request.trigger_source,
            metrics_only=request.metrics_only,
            fixture_path="",
            started_at=utc_timestamp(started),
            finished_at="",
            window_start=utc_timestamp(window_start),
            window_end=utc_timestamp(window_end),
            trade_count=0,
            buy_volume="0",
            sell_volume="0",
            delta="0",
            cvd="0",
            data_quality="MISSING",
            manifest_path="",
            parquet_rows=0,
            created_at=utc_timestamp(started),
            source_mode=source_mode,
            batch_id=request.batch_id or resolved_scan_id,
        )
        store.save_scan(scan)
        current_stage = "QUEUED"
        try:
            # DATA: resample persisted agg_trades into immutable UTC bars.
            self._stage("DATA")
            current_stage = "DATA"
            resampling = ResamplingService(
                settings=container.settings,
                store=store,
                parquet=container.parquet,
                clock=clock,
            )
            persisted_bars = resampling.resample_and_persist(
                BarResampleRequest(
                    symbol=request.symbol,
                    period=request.period,
                    window_start=window_start,
                    window_end=window_end,
                    source_mode=source_mode,
                )
            )

            # METRICS: compute the versioned document + footprint parts.
            self._stage("METRICS")
            current_stage = "METRICS"
            metric_request = MetricRequest(
                symbol=request.symbol,
                period=request.period,
                window_start=window_start,
                window_end=window_end,
                tick_size=tick_size,
            )
            inputs = self._inputs
            if inputs is None:
                inputs = measured_liquidation_inputs(container, metric_request)
            metrics_service = MetricService(
                settings=container.settings,
                store=store,
                parquet=container.parquet,
                clock=clock,
            )
            persisted_metric = metrics_service.compute_and_persist(metric_request, inputs=inputs)
            document = persisted_metric.document

            # LLM (optional): analyze the snapshot, then freeze the report with
            # whatever CALC + LLM state was produced.
            self._stage("LLM")
            current_stage = "LLM"
            finished = clock.now()
            snapshot_id = _snapshot_id(
                request.symbol,
                finished,
                window_start=utc_timestamp(window_start),
                window_end=utc_timestamp(window_end),
            )
            policy = _window_policy(
                period=request.period,
                window_start=window_start,
                window_end=window_end,
                requested=request.start is not None or request.end is not None,
            )
            snapshot = build_market_snapshot(
                document=document,
                bars=_snapshot_bars(persisted_bars.run.result.bars, document),
                contract=contract,
                snapshot_id=snapshot_id,
                source_mode=source_mode,
            )
            manifest_entries = [persisted_bars.entry, persisted_metric.entry]
            if persisted_metric.footprint_entry is not None:
                manifest_entries.append(persisted_metric.footprint_entry)
            report_id = _report_id(request.symbol, finished)
            llm_state, decision_id, narrative_ref, opportunity_ids = self._llm_stage(
                request=request,
                container=container,
                document=document,
                snapshot=snapshot,
                scan_id=scan.scan_id,
                report_id=report_id,
            )
            self._stage("REPORT")
            current_stage = "REPORT"
            if opportunity_ids:
                llm_state = {**llm_state, "opportunity_ids": list(opportunity_ids)}
            tokens, cost_usd, cost_unpriced = _llm_totals(llm_state)
            symbol_stage = scan_symbol_stage(llm_state)
            report = ReportRecord(
                report_id=report_id,
                scan_id=scan.scan_id,
                symbol=request.symbol,
                timeframe=request.period,
                generated_at=utc_timestamp(finished),
                metrics_manifest_json=json.dumps([asdict(entry) for entry in manifest_entries]),
                llm_decision_id=decision_id,
                narrative_ref=narrative_ref,
                data_quality=document.quality.data_quality.value,
                snapshot_json=json.dumps(snapshot),
                metrics_json=document.json_text,
                llm_json=json.dumps(llm_state),
                window_policy_json=json.dumps(policy),
            )
            summary = _summary_values(document)
            completed = replace(
                scan,
                status="DONE",
                finished_at=report.generated_at,
                trade_count=sum(bar.trade_count for bar in persisted_bars.run.result.bars),
                buy_volume=summary["buy_volume"],
                sell_volume=summary["sell_volume"],
                delta=summary["delta"],
                cvd=summary["cvd"],
                data_quality=report.data_quality,
                manifest_path=persisted_bars.entry.path,
                parquet_rows=persisted_bars.entry.rows,
                report_id=report.report_id,
                metrics_manifest_json=report.metrics_manifest_json,
                llm_configured=bool(llm_state.get("configured")),
                llm_status=scan_llm_status(llm_state),
                llm_tokens=tokens,
                cost_usd=cost_usd,
                cost_unpriced=cost_unpriced,
            )
            store.save_report_and_complete_scan(report, completed)
            return ScanRun(
                scan=completed,
                report=report,
                llm_state=llm_state,
                symbol_stage=symbol_stage,
                llm_tokens=tokens,
                cost_usd=cost_usd,
                cost_unpriced=cost_unpriced,
            )
        except Exception as exc:
            # Record which stage failed so a FAILED checkpoint row renders the
            # correct DATA/METRICS/LLM/REPORT cells instead of assuming METRICS.
            with suppress(Exception):
                cast(Any, exc).failed_stage = current_stage
            failed = replace(
                scan,
                status="FAILED",
                finished_at=utc_timestamp(clock.now()),
                data_quality="MISSING",
                error_code=str(getattr(exc, "code", "SCAN_FAILED")),
                error_message=str(exc),
            )
            with suppress(Exception):
                # Preserve the original failure if the status update also fails.
                store.update_scan(failed)
            raise

    def _llm_stage(
        self,
        *,
        request: ScanRequest,
        container: ScanContainer,
        document: Any,
        snapshot: dict[str, object],
        scan_id: str,
        report_id: str,
    ) -> tuple[dict[str, Any], str | None, str | None, tuple[str, ...]]:
        """Run the optional LLM stage; never fail the scan when it is unavailable."""
        if request.metrics_only:
            return {"configured": False, "reason": LLM_DISABLED_REASON}, None, None, ()
        # Batch and scheduled runs construct one service over many symbols; read
        # the provider from a freshly resolved snapshot so the first symbol does
        # not use the provider/key that was active before an operator change.
        container.refresh_llm_settings()
        provider_name = (request.provider or container.settings.llm_provider or "").strip()
        if request.source == "live":
            verdict = check_live_source(container.settings, container.clock)
            if not verdict.fresh:
                return (
                    {
                        "configured": False,
                        "reason": f"LIVE_DATA_STALE: {verdict.reason}",
                    },
                    None,
                    None,
                    (),
                )
        if not provider_name:
            return (
                {"configured": False, "reason": LLM_PROVIDER_NOT_CONFIGURED_REASON},
                None,
                None,
                (),
            )
        provider = container.provider_for(provider_name)
        if provider is None:
            return (
                {"configured": False, "reason": LLM_PROVIDER_NOT_CONFIGURED_REASON},
                None,
                None,
                (),
            )
        if not provider_configured(container.settings, provider_name):
            # A named provider without a key must not call out and degrade with
            # an auth error; report the explicit configuration reason (M16).
            return (
                {"configured": False, "reason": LLM_PROVIDER_NOT_CONFIGURED_REASON},
                None,
                None,
                (),
            )
        try:
            # Constructing the service (and loading the price table lazily) is
            # inside the guarded block so a corrupt operator table degrades the
            # scan to CALC instead of failing it (M11).
            service = AnalysisService(
                store=container.store,
                clock=container.clock,
                provider=provider,
                settings=container.settings,
                runtime=container.llm_runtime,
            )
            outcome = service.analyze(
                symbol=request.symbol,
                timeframe=request.period,
                metrics=json.loads(document.json_text),
                snapshot=snapshot,
                scan_id=scan_id,
                report_id=report_id,
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never abort the batch
            logger.warning(
                "scan_llm_stage_failed",
                symbol=request.symbol,
                scan_id=scan_id,
                error=type(exc).__name__,
            )
            error_code = str(getattr(exc, "code", "") or "")
            prefix = "CONFIG_INVALID" if error_code == "CONFIG_INVALID" else "LLM_STAGE_FAILED"
            return (
                {
                    "configured": True,
                    "provider": provider_name,
                    "validation_status": "REJECTED",
                    "reason": f"{prefix}: {exc}",
                    # Additive: replay branches on the typed code instead of
                    # sniffing the English reason string.
                    "error_code": error_code,
                    "opportunity_ids": [],
                },
                None,
                None,
                (),
            )
        tokens = 0
        for call in outcome.calls:
            with suppress(json.JSONDecodeError, TypeError, ValueError):
                usage = json.loads(call.usage_json)
                if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
                    tokens += int(usage["total_tokens"])
        cost_total = Decimal("0")
        for entry in outcome.budget:
            try:
                cost_total += Decimal(str(entry.record.cost_usd))
            except InvalidOperation:
                continue
        unpriced = (
            any(entry.record.unpriced for entry in outcome.budget) if outcome.budget else True
        )
        llm_state = dict(outcome.llm_state)
        llm_state["usage"] = {"total_tokens": tokens}
        cost_state = dict(llm_state.get("cost") or {})
        cost_state["amount_usd"] = format(cost_total, "f")
        cost_state["unpriced"] = unpriced
        llm_state["cost"] = cost_state
        narrative_ref = f"inline:{outcome.decision_id}"
        return llm_state, outcome.decision_id, narrative_ref, outcome.opportunity_ids


def _public_manifest_entries(value: object) -> object:
    if not isinstance(value, list):
        return value
    entries: list[object] = []
    for item in value:
        if isinstance(item, dict):
            public = dict(item)
            if isinstance(public.get("path"), str):
                public["path"] = Path(public["path"]).name
            entries.append(public)
        else:
            entries.append(item)
    return entries


def public_scan_payload(record: ScanRecord) -> dict[str, object]:
    """Scan JSON for API/CLI without exposing daemon-absolute filesystem paths."""
    payload = asdict(record)
    if payload.get("manifest_path"):
        payload["manifest_path"] = Path(str(payload["manifest_path"])).name
    manifest_json = payload.get("metrics_manifest_json")
    if isinstance(manifest_json, str) and manifest_json:
        with suppress(json.JSONDecodeError):
            payload["metrics_manifest_json"] = json.dumps(
                _public_manifest_entries(json.loads(manifest_json))
            )
    degraded = payload.get("status") == "DONE" and payload.get("llm_status") == "REJECTED"
    payload["degraded"] = degraded
    payload["symbol_stage"] = "DEGRADED" if degraded else payload.get("status")
    return payload


def _llm_state(report: ReportRecord) -> dict[str, Any]:
    if not report.llm_json:
        return {"configured": False, "reason": LLM_DISABLED_REASON}
    try:
        value = json.loads(report.llm_json)
    except json.JSONDecodeError:
        return {"configured": False, "reason": LLM_DISABLED_REASON}
    return (
        value if isinstance(value, dict) else {"configured": False, "reason": LLM_DISABLED_REASON}
    )


def _report_window_policy(report: ReportRecord, snapshot: dict[str, Any]) -> Any:
    if report.window_policy_json:
        with suppress(json.JSONDecodeError):
            value = json.loads(report.window_policy_json)
            if isinstance(value, dict):
                return value
    window = snapshot.get("window")
    if isinstance(window, dict) and window.get("start") and window.get("end"):
        try:
            start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        return _window_policy(
            period=str(report.timeframe),
            window_start=start,
            window_end=end,
            requested=False,
        )
    return None


def report_payload(report: ReportRecord) -> dict[str, object]:
    """Render one stored report for the API and CLI (identical sections)."""
    snapshot = json.loads(report.snapshot_json)
    llm = _llm_state(report)
    tokens, cost_usd, cost_unpriced = _llm_totals(llm)
    return {
        "report_id": report.report_id,
        "scan_id": report.scan_id,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "generated_at": report.generated_at,
        "data_quality": report.data_quality,
        "llm_decision_id": report.llm_decision_id,
        "narrative_ref": report.narrative_ref,
        "llm": llm,
        "llm_stage": scan_llm_stage(llm),
        "llm_tokens": tokens,
        "cost_usd": cost_usd,
        "cost_unpriced": cost_unpriced,
        "window_policy": _report_window_policy(report, snapshot),
        "delivery": {
            "configured": False,
            "reason": "TELEGRAM_NOT_CONFIGURED: Telegram delivery is not configured in this build.",
        },
        "metrics_manifest": _public_manifest_entries(json.loads(report.metrics_manifest_json)),
        "snapshot": snapshot,
        "metrics": json.loads(report.metrics_json),
    }


def report_summary(report: ReportRecord) -> dict[str, object]:
    """Compact report row used by ``GET /api/v1/reports`` and the scans payload."""
    llm = _llm_state(report)
    tokens, cost_usd, cost_unpriced = _llm_totals(llm)
    return {
        "report_id": report.report_id,
        "scan_id": report.scan_id,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "generated_at": report.generated_at,
        "data_quality": report.data_quality,
        "llm_decision_id": report.llm_decision_id,
        "narrative_ref": report.narrative_ref,
        "llm_stage": scan_llm_stage(llm),
        "llm_tokens": tokens,
        "cost_usd": cost_usd,
        "cost_unpriced": cost_unpriced,
        "llm": {
            "configured": llm.get("configured", False),
            "validation_status": llm.get("validation_status"),
            "opportunity_ids": llm.get("opportunity_ids", []),
        },
    }


__all__ = [
    "LLM_DISABLED_REASON",
    "SCAN_STAGES",
    "SUPPORTED_SOURCES",
    "TERMINAL_STAGES",
    "ScanAlreadyRunning",
    "ScanContainer",
    "ScanDataUnavailable",
    "ScanError",
    "ScanMetricsIncomplete",
    "ScanRequest",
    "ScanRun",
    "ScanService",
    "ScanValidationError",
    "build_market_snapshot",
    "measured_liquidation_inputs",
    "public_scan_payload",
    "report_payload",
    "report_summary",
    "scan_llm_status",
    "scan_llm_stage",
    "scan_symbol_stage",
]
