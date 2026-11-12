"""Deterministic replay harness.

The harness runs the *same* pure metric functions as live, but it never opens
the daemon store, never reads Parquet, and never calls the wall clock. It reads
one event source, advances a :class:`~oft.replay.clock.ReplayClock` in event
time, and writes one immutable JSON document under
``$OFT_DATA/replay/<symbol>/<period>/<metric_version>/<input_manifest_hash>.json``.

Invariants (ADR-0011, slice 10):

* same events + same metric version -> identical metric hash;
* a different metric version -> a new path; existing files are never edited;
* re-running the same inputs is idempotent;
* a computation that is not deterministic fails loudly;
* nothing outside the replay namespace is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from oft.application.metric_service import (
    SCHEMA_VERSION,
    MetricRequest,
    input_manifest_hash,
)
from oft.domain.events import AggTrade
from oft.domain.metrics.cvd import compute_cvd
from oft.domain.metrics.footprint import (
    DEFAULT_IMBALANCE_THRESHOLD,
    DEFAULT_STACKED_MIN_LEVELS,
    DEFAULT_TOP_IMBALANCES,
    compute_footprint,
)
from oft.domain.metrics.liquidations import LIMITATIONS
from oft.domain.metrics.types import Unit, contract_json, decimal_str
from oft.domain.metrics.version import MetricVersion, orderflow_metric_version
from oft.domain.metrics.volume_profile import (
    DEFAULT_VALUE_AREA_PCT,
    compute_volume_profile,
)
from oft.domain.time import utc_timestamp
from oft.replay.clock import ReplayClock
from oft.replay.event_source import ReplayEventSource, ReplayWindow

REPLAY_NAMESPACE = "replay"
PAPER_LOOP_NAMESPACE = "paper-loop"
DEFAULT_REPLAY_ENVIRONMENT = "PAPER"
DEFAULT_REPLAY_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
_READ_ONLY_ENVIRONMENTS = frozenset({"PAPER", "TEST"})


class ReplayError(ValueError):
    """The replay configuration or event source is unusable."""


class ReplayNonDeterministicError(ReplayError):
    """Two identical computations disagreed; replay must fail loudly."""


class ReplayLivePathError(ReplayError):
    """Replay refuses any configuration that could reach a live order path."""


@dataclass(frozen=True)
class ReplayConfig:
    """Inputs that fully determine one replay document."""

    symbol: str
    period: str
    window_start: datetime
    window_end: datetime
    tick_size: Decimal
    unit: Unit = "BASE"
    bin_size_multiple: int = 1
    value_area_pct: Decimal = DEFAULT_VALUE_AREA_PCT
    cvd_algorithm_version: int = 1
    volume_profile_algorithm_version: int = 1
    footprint_algorithm_version: int = 1
    open_interest_algorithm_version: int = 1
    funding_algorithm_version: int = 1
    liquidation_algorithm_version: int = 1

    def __post_init__(self) -> None:
        ReplayWindow(symbol=self.symbol, start=self.window_start, end=self.window_end)
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite():
            raise ReplayError("tick_size must be a finite Decimal")
        if self.tick_size <= 0:
            raise ReplayError("tick_size must be positive")


@dataclass(frozen=True)
class ReplayResult:
    """The identity and location of one deterministic replay document."""

    metric_version: str
    input_manifest_hash: str
    metric_hash: str
    computed_at: str
    event_count: int
    data_quality: str
    output_path: str
    idempotent: bool = False


@dataclass(frozen=True)
class _Computation:
    metric_version: str
    input_manifest_hash: str
    metric_hash: str
    json_text: str
    payload: dict[str, Any]
    computed_at: str
    event_count: int


def _cvd_payload(cvd: Any) -> dict[str, Any]:
    return {
        "unit": cvd.unit,
        "points": [
            {
                "bar_start": utc_timestamp(point.bar_start),
                "bar_end": utc_timestamp(point.bar_end),
                "buy_volume": decimal_str(point.buy_volume),
                "sell_volume": decimal_str(point.sell_volume),
                "delta": decimal_str(point.delta),
                "cvd": decimal_str(point.cvd),
                "taker_buy_ratio": point.taker_buy_ratio,
            }
            for point in cvd.points
        ],
        "summary": {
            "delta": decimal_str(cvd.summary.delta),
            "cvd_change": decimal_str(cvd.summary.cvd_change),
            "buy_volume": decimal_str(cvd.summary.buy_volume),
            "sell_volume": decimal_str(cvd.summary.sell_volume),
        },
        "divergence_flags": [
            {
                "bar_start": utc_timestamp(flag.bar_start),
                "kind": flag.kind,
                "note": flag.note,
            }
            for flag in cvd.divergence_flags
        ],
    }


def _volume_profile_payload(profile: Any) -> dict[str, Any]:
    return {
        "window": {
            "start": utc_timestamp(profile.window.start),
            "end": utc_timestamp(profile.window.end),
        },
        "bin_size": decimal_str(profile.bin_size),
        "value_area_pct": profile.value_area_pct,
        "poc": decimal_str(profile.poc),
        "vah": decimal_str(profile.vah),
        "val": decimal_str(profile.val),
        "bins": [
            {
                "price_low": decimal_str(bin_.price_low),
                "price_high": decimal_str(bin_.price_high),
                "volume": decimal_str(bin_.volume),
                "buy_volume": decimal_str(bin_.buy_volume),
                "sell_volume": decimal_str(bin_.sell_volume),
                "is_poc": bin_.is_poc,
                "in_value_area": bin_.in_value_area,
            }
            for bin_ in profile.bins
        ],
        "notes": list(profile.notes),
    }


def _empty_open_interest(window_end: datetime) -> dict[str, Any]:
    return {
        "unit": "BASE",
        "sampling": "REGULAR",
        "points": [],
        "delta_over_window": "0",
        "last": {
            "ts": utc_timestamp(window_end),
            "open_interest": "0",
            "open_interest_value": "0",
        },
    }


def _empty_funding(window_end: datetime) -> dict[str, Any]:
    return {
        "current_rate": "0",
        "next_funding_time": utc_timestamp(window_end),
        "interval_hours": 8,
        "mark_price": "0",
        "index_price": "0",
        "history": [],
    }


def _empty_liquidations() -> dict[str, Any]:
    return {
        "coverage": "NONE",
        "coverage_start": None,
        "coverage_end": None,
        "total_long_notional": "0",
        "total_short_notional": "0",
        "count": 0,
        "buckets": [],
        "limitations": list(LIMITATIONS),
    }


class ReplayHarness:
    """Compute and publish one deterministic replay document."""

    def __init__(
        self,
        *,
        data_dir: Path,
        source: ReplayEventSource,
        config: ReplayConfig,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._source = source
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    def _request(self) -> MetricRequest:
        return MetricRequest(
            symbol=self._config.symbol,
            period=self._config.period,
            window_start=self._config.window_start,
            window_end=self._config.window_end,
            tick_size=self._config.tick_size,
            bin_size_multiple=self._config.bin_size_multiple,
            value_area_pct=self._config.value_area_pct,
            unit=self._config.unit,
        )

    def _version(self) -> MetricVersion:
        return orderflow_metric_version(
            period=self._config.period,
            unit=self._config.unit,
            tick_size=self._config.tick_size,
            value_area_pct=self._config.value_area_pct,
            bin_size_multiple=self._config.bin_size_multiple,
            cvd_algorithm_version=self._config.cvd_algorithm_version,
            volume_profile_algorithm_version=self._config.volume_profile_algorithm_version,
            footprint_algorithm_version=self._config.footprint_algorithm_version,
            open_interest_algorithm_version=self._config.open_interest_algorithm_version,
            funding_algorithm_version=self._config.funding_algorithm_version,
            liquidation_algorithm_version=self._config.liquidation_algorithm_version,
            footprint_imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
            footprint_stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
            footprint_top_imbalances=DEFAULT_TOP_IMBALANCES,
        )

    def _payload(
        self,
        request: MetricRequest,
        version: MetricVersion,
        input_hash: str,
        computed_at: str,
        events: tuple[AggTrade, ...],
    ) -> dict[str, Any]:
        cvd = compute_cvd(
            events,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            unit=request.unit,
        )
        footprint = compute_footprint(
            events,
            symbol=request.symbol,
            period=request.period,
            window_start=request.window_start,
            window_end=request.window_end,
            tick_size=request.tick_size,
            bucket_multiple=request.bin_size_multiple,
            imbalance_threshold=DEFAULT_IMBALANCE_THRESHOLD,
            stacked_min_levels=DEFAULT_STACKED_MIN_LEVELS,
            top_imbalances=DEFAULT_TOP_IMBALANCES,
        )
        profile = compute_volume_profile(
            events,
            symbol=request.symbol,
            window_start=request.window_start,
            window_end=request.window_end,
            tick_size=request.tick_size,
            bin_size_multiple=request.bin_size_multiple,
            value_area_pct=request.value_area_pct,
        )
        if events:
            data_quality = "PARTIAL"
            quality_reasons = [
                "open_interest not measured in this slice",
                "funding not measured in this slice",
            ]
        else:
            data_quality = "MISSING"
            quality_reasons = ["no aggTrade rows in the replay window"]
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": request.symbol,
            "period": request.period,
            "window": {
                "start": utc_timestamp(request.window_start),
                "end": utc_timestamp(request.window_end),
            },
            "computed_at": computed_at,
            "metric_version": version.identifier,
            "input_manifest_hash": input_hash,
            "data_quality": data_quality,
            "quality_reasons": quality_reasons,
            "cvd": _cvd_payload(cvd),
            "footprint": footprint.to_contract(),
            "volume_profile": _volume_profile_payload(profile),
            "open_interest": _empty_open_interest(request.window_end),
            "funding": _empty_funding(request.window_end),
            "liquidations": _empty_liquidations(),
            "sources": [
                {
                    "kind": "REPLAY",
                    "ref": (
                        f"{request.symbol}/{request.period}/{utc_timestamp(request.window_start)}"
                    ),
                    "as_of": computed_at,
                    "version": version.identifier,
                    "hash": input_hash,
                }
            ],
            "gaps": [],
        }

    def _compute(
        self,
        events: tuple[AggTrade, ...],
        request: MetricRequest,
        version: MetricVersion,
        input_hash: str,
        computed_at: str,
    ) -> _Computation:
        payload = self._payload(request, version, input_hash, computed_at, events)
        json_text = contract_json(payload)
        metric_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
        return _Computation(
            metric_version=version.identifier,
            input_manifest_hash=input_hash,
            metric_hash=metric_hash,
            json_text=json_text,
            payload=payload,
            computed_at=computed_at,
            event_count=len(events),
        )

    def _output_path(self, computation: _Computation) -> Path:
        path = (
            self._replay_root
            / self._config.symbol
            / self._config.period
            / computation.metric_version
            / f"{computation.input_manifest_hash}.json"
        )
        resolved = path.resolve()
        if not resolved.is_relative_to(self._replay_root):
            raise ReplayError("replay output escaped the replay namespace")
        return resolved

    def _record(self, computation: _Computation) -> str:
        return contract_json(
            {
                "metric_hash": computation.metric_hash,
                "metric_version": computation.metric_version,
                "input_manifest_hash": computation.input_manifest_hash,
                "computed_at": computation.computed_at,
                "event_count": computation.event_count,
                "data_quality": computation.payload["data_quality"],
                "document": computation.payload,
            }
        )

    def run(self) -> ReplayResult:
        """Replay the configured window, write once, and return its identity."""
        if (
            self._source.window.symbol != self._config.symbol
            or self._source.window.start != self._config.window_start
            or self._source.window.end != self._config.window_end
        ):
            raise ReplayError("event source window does not match the replay config")

        events = tuple(self._source.iter_events())
        clock = ReplayClock(self._config.window_start)
        for event in events:
            clock.advance_to(event.transact_time)
        computed_at = utc_timestamp(clock.now())

        request = self._request()
        version = self._version()
        input_hash = input_manifest_hash(events, request)
        first = self._compute(events, request, version, input_hash, computed_at)
        second = self._compute(events, request, version, input_hash, computed_at)
        if first.metric_hash != second.metric_hash:
            raise ReplayNonDeterministicError(
                "replay computation is non-deterministic: "
                f"{first.metric_hash} != {second.metric_hash}"
            )

        output_path = self._output_path(first)
        if output_path.exists():
            try:
                existing = json.loads(output_path.read_text(encoding="utf-8"), parse_float=Decimal)
            except (OSError, json.JSONDecodeError) as exc:
                raise ReplayNonDeterministicError(
                    f"existing replay document is unreadable: {output_path.name}"
                ) from exc
            if not isinstance(existing, dict):
                raise ReplayNonDeterministicError("existing replay document is not a JSON object")
            existing_hash = existing.get("metric_hash")
            existing_document = existing.get("document")
            if not isinstance(existing_document, dict):
                raise ReplayNonDeterministicError("existing replay document has no document body")
            recomputed_hash = hashlib.sha256(
                contract_json(existing_document).encode("utf-8")
            ).hexdigest()
            if existing_hash != recomputed_hash or existing_hash != first.metric_hash:
                raise ReplayNonDeterministicError(
                    "existing replay document is tampered or has a different metric hash; "
                    "refusing to edit it in place"
                )
            return ReplayResult(
                metric_version=first.metric_version,
                input_manifest_hash=first.input_manifest_hash,
                metric_hash=first.metric_hash,
                computed_at=first.computed_at,
                event_count=first.event_count,
                data_quality=first.payload["data_quality"],
                output_path=str(output_path),
                idempotent=True,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self._record(first))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, output_path)
            directory_fd = os.open(output_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return ReplayResult(
            metric_version=first.metric_version,
            input_manifest_hash=first.input_manifest_hash,
            metric_hash=first.metric_hash,
            computed_at=first.computed_at,
            event_count=first.event_count,
            data_quality=first.payload["data_quality"],
            output_path=str(output_path),
        )


# -- paper-loop replay -------------------------------------------------------
#
# The metric harness above replays one metrics window. The paper-loop replay
# below runs the *full* deterministic path (backfill -> scan -> trigger ->
# entry re-check -> risk -> paper fill) twice in two fresh stores that both
# live under ``$OFT_DATA/replay/paper-loop/<run_id>/``. The live database,
# paper account, OMS ledger, and budget ledger are never opened or written.


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class PaperLoopConfig:
    """Inputs that fully determine one paper-loop replay."""

    symbol: str
    archive_fixture_dir: Path
    backfill_start: date
    backfill_end: date
    period: str = "4h"
    recordings_dir: Path | None = None
    live_llm: bool = False
    capture_recordings: bool = False
    provider: str | None = None
    environment: str = DEFAULT_REPLAY_ENVIRONMENT
    now: datetime = DEFAULT_REPLAY_NOW
    run_id: str = "paper-loop"
    llm_fixtures_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.environment.strip().upper() not in _READ_ONLY_ENVIRONMENTS:
            raise ReplayLivePathError(
                f"replay refuses environment {self.environment!r}; "
                "only PAPER/TEST replay is supported and a live order path is never opened"
            )
        if self.now.tzinfo is None:
            raise ReplayError("paper-loop replay requires a timezone-aware fixed clock")
        if self.backfill_end < self.backfill_start:
            raise ReplayError("backfill_end must be on or after backfill_start")
        if _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ReplayError("run_id must match ^[A-Za-z0-9._-]{1,64}$")
        if self.recordings_dir is None:
            raise ReplayError(
                "recordings_dir is required; a replay never invents an LLM output. "
                "Capture one with --capture or --live-llm first."
            )


@dataclass(frozen=True)
class PaperLoopOutcome:
    """The stable values one attempt produced (never a path or random id)."""

    decision_id: str
    decision_validation: str
    metric_version: str | None
    prompt_version: int | None
    knowledge_artifact_hash: str | None
    llm_provider: str | None
    llm_model: str | None
    data_quality: str
    opportunity: dict[str, Any]
    trigger: dict[str, Any]
    order: dict[str, Any] | None
    fill: dict[str, Any] | None
    position: dict[str, Any] | None
    account: dict[str, Any] | None
    risk_verdict: dict[str, Any]


@dataclass(frozen=True)
class PaperLoopResult:
    """The identity of one verified deterministic paper-loop replay."""

    run_id: str
    trigger_id: str
    decision_id: str
    decision_validation: str
    order_id: str | None
    fill_id: str | None
    position_id: str | None
    entry_price: str | None
    fee: str | None
    position_size: str | None
    position_value: str | None
    summary_path: str
    deterministic: bool = True


def _stable_opportunity(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the two ids a scan generates randomly; everything else is pinned."""
    copy: dict[str, Any] = json.loads(json.dumps(payload))
    source = copy.get("source")
    if isinstance(source, dict):
        copy["source"] = {
            key: value for key, value in source.items() if key not in {"scan_id", "report_id"}
        }
    return copy


def _stable_outcome(outcome: PaperLoopOutcome) -> dict[str, Any]:
    """The projection two replay attempts must agree on exactly."""
    return {
        "decision_id": outcome.decision_id,
        "decision_validation": outcome.decision_validation,
        "metric_version": outcome.metric_version,
        "prompt_version": outcome.prompt_version,
        "knowledge_artifact_hash": outcome.knowledge_artifact_hash,
        "llm_provider": outcome.llm_provider,
        "llm_model": outcome.llm_model,
        "data_quality": outcome.data_quality,
        "opportunity": _stable_opportunity(outcome.opportunity),
        "trigger": outcome.trigger,
        "order": outcome.order,
        "fill": outcome.fill,
        "position": outcome.position,
        "account": outcome.account,
        "risk_verdict": outcome.risk_verdict,
    }


def _canonical_json(payload: Any) -> str:
    """Deterministic comparison text; the paper loop carries floats, unlike metrics."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


class PaperLoopReplay:
    """Run the paper loop twice under replay and prove it is deterministic."""

    def __init__(self, *, data_dir: Path, config: PaperLoopConfig) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    @property
    def run_root(self) -> Path:
        return self._replay_root / PAPER_LOOP_NAMESPACE / self._config.run_id

    def _settings(self, attempt_dir: Path) -> Any:
        from oft.config import DEFAULT_LLM_FIXTURES_DIR, Settings

        values: dict[str, Any] = {
            "env": "test",
            "port": 8765,
            "data_dir": attempt_dir,
            "archive_fixture_dir": self._config.archive_fixture_dir,
            "llm_model": "fixture",
            "llm_fixtures_dir": self._config.llm_fixtures_dir or DEFAULT_LLM_FIXTURES_DIR,
        }
        if self._config.live_llm:
            # Honour the configured provider (explicit --provider wins), never
            # default to a live provider when none is named.
            if self._config.provider is not None:
                values["llm_provider"] = self._config.provider
        else:
            values["llm_provider"] = "stub"
        return Settings(**values)

    def _recordings_dir(self) -> Path:
        assert self._config.recordings_dir is not None
        return Path(self._config.recordings_dir).expanduser().resolve()

    def _source(self, container: Any, settings: Any) -> Any:
        from oft.adapters.llm_stub import LLMStub
        from oft.application.prompt_service import secret_values_from_settings
        from oft.replay.llm_source import RecordedLLMSource, RecordingLLMSource

        if not (self._config.live_llm or self._config.capture_recordings):
            return RecordedLLMSource(recordings_dir=self._recordings_dir())
        if self._config.live_llm:
            inner = container.llm_provider
            if inner is None:
                raise ReplayError(
                    "no live LLM provider is configured; set OFT_LLM_PROVIDER or omit --live-llm"
                )
        else:
            inner = LLMStub(fixtures_dir=settings.llm_fixtures_dir)
        return RecordingLLMSource(
            inner=inner,
            recordings_dir=self._recordings_dir(),
            clock=container.clock,
            secrets=secret_values_from_settings(settings),
        )

    @staticmethod
    def _provider_name(source: Any) -> str:
        return str(getattr(source, "provider", "") or "stub")

    def _run_once(self, attempt: str) -> PaperLoopOutcome:
        from dataclasses import replace as replace_dataclass

        from oft.application.entry_service import EntryService
        from oft.application.kill_switch import KillSwitch
        from oft.application.scan_service import ScanRequest, ScanService
        from oft.application.trigger_service import TriggerService
        from oft.bootstrap import build_container
        from oft.clock import FixedClock

        attempt_dir = self.run_root / attempt
        settings = self._settings(attempt_dir)
        clock = FixedClock(self._config.now)
        container = build_container(settings, initialize=True, clock=clock)
        source = self._source(container, settings)
        container = replace_dataclass(container, llm_provider=source)
        provider_name = self._provider_name(source)

        backfill = container.backfill.run(
            symbol=self._config.symbol,
            data_type="aggTrades",
            start=self._config.backfill_start,
            end=self._config.backfill_end,
        )
        if backfill.run.status != "COMPLETE":
            raise ReplayError(f"replay backfill did not complete: {backfill.run.status}")

        scan = ScanService(container=container, lock=container.scan_lock).run(
            ScanRequest(
                symbol=self._config.symbol,
                period=self._config.period,
                metrics_only=False,
                provider=provider_name,
            )
        )
        opportunities = container.store.list_opportunities(state="OPEN")
        if not opportunities:
            self._raise_no_opportunity(scan)
        opportunity = opportunities[0]

        trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
            opportunity.opportunity_id
        )
        if trigger_run.trigger is None:
            raise ReplayError(
                "replay trigger did not fire; the fixture window is not replayable: "
                + "; ".join(trigger_run.evaluation.reasons)
            )
        trigger_id = trigger_run.trigger.trigger_id
        kill = KillSwitch(
            settings=container.settings,
            store=container.store,
            clock=container.clock,
        )
        entry = EntryService(
            store=container.store,
            clock=container.clock,
            provider=source,
            settings=container.settings,
            kill_switch=kill,
        )
        result = entry.accept(trigger_id, idempotency_key=f"replay-{self._config.run_id}")

        stored_trigger = container.store.get_trigger_event(trigger_id)
        if stored_trigger is None:  # pragma: no cover - accept writes it atomically
            raise ReplayError("replay trigger disappeared after accept")
        trigger_payload = json.loads(stored_trigger.payload_json)
        decision = container.store.get_decision(result.decision_id)
        decision_payload = {} if decision is None else json.loads(decision.payload_json)
        provenance = decision_payload.get("provenance") or {}
        metrics_payload = json.loads(scan.report.metrics_json)

        return PaperLoopOutcome(
            decision_id=result.decision_id,
            decision_validation=str(decision_payload.get("validation_status") or ""),
            metric_version=metrics_payload.get("metric_version"),
            prompt_version=provenance.get("prompt_version"),
            knowledge_artifact_hash=provenance.get("knowledge_artifact_hash"),
            llm_provider=provenance.get("provider"),
            llm_model=provenance.get("model"),
            data_quality=str(trigger_payload.get("data_quality") or ""),
            opportunity=json.loads(opportunity.payload_json),
            trigger=trigger_payload,
            order=result.order,
            fill=result.fill,
            position=result.position,
            account=result.account,
            risk_verdict=result.risk_verdict,
        )

    @staticmethod
    def _raise_no_opportunity(scan: Any) -> None:
        llm_state = getattr(scan, "llm_state", {}) or {}
        reason = str(llm_state.get("reason") or "")
        if "recorded LLM output" in reason or "LLM_RECORDING_MISSING" in reason:
            raise ReplayError(
                "no recorded LLM output for role=ANALYSIS; record one or pass --live-llm: " + reason
            )
        raise ReplayError("replay produced no OPEN opportunity; " + (reason or "unknown reason"))

    def run(self) -> PaperLoopResult:
        """Run the loop twice, compare every stable value, and write a summary."""
        first = self._run_once("attempt-1")
        second = self._run_once("attempt-2")
        left = _stable_outcome(first)
        right = _stable_outcome(second)
        if _canonical_json(left) != _canonical_json(right):
            raise ReplayNonDeterministicError(
                "paper-loop replay is non-deterministic: attempt-1 and attempt-2 produced "
                "different decisions, triggers, orders, fills, positions, or account values"
            )

        summary_path = (self.run_root / "summary.json").resolve()
        if not summary_path.is_relative_to(self._replay_root.resolve()):
            raise ReplayError("replay summary escaped the replay namespace")
        summary = {
            "run_id": self._config.run_id,
            "environment": self._config.environment.upper(),
            "symbol": self._config.symbol,
            "period": self._config.period,
            "attempts": 2,
            "deterministic": True,
            "outcome": left,
        }
        _write_atomic(summary_path, _canonical_json(summary))

        order = first.order or {}
        fill = first.fill or {}
        position = first.position or {}
        return PaperLoopResult(
            run_id=self._config.run_id,
            trigger_id=str(first.trigger.get("trigger_id") or ""),
            decision_id=first.decision_id,
            decision_validation=first.decision_validation,
            order_id=None if first.order is None else str(order.get("order_id") or ""),
            fill_id=None if first.fill is None else str(fill.get("fill_id") or ""),
            position_id=None if first.position is None else str(position.get("position_id") or ""),
            entry_price=None if first.fill is None else str(fill.get("price") or ""),
            fee=None if first.fill is None else str(fill.get("fee") or ""),
            position_size=(None if first.position is None else str(position.get("size") or "")),
            position_value=(
                None if first.position is None else str(position.get("notional_usd") or "")
            ),
            summary_path=str(summary_path),
        )
