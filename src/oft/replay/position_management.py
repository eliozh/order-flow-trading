"""Deterministic position-management, journal, and review replay.

The metric harness replays one metrics window and :mod:`oft.replay.paper_loop`
replays the entry loop. This module replays the *whole* Milestone 3 loop in one
scenario: entry -> recorded MANAGEMENT decisions (PARTIAL_CLOSE, ADD, HOLD,
FULL_CLOSE) -> recorded funding accrual -> full close -> trade log -> journal
statistics -> recorded REVIEW. It runs the same domain code as live twice in
two fresh stores under ``$OFT_DATA/replay/position-management/<run_id>/`` and
compares every stable value.

Safety rules (shared with the paper-loop replay):

* the run root must stay under the replay namespace and symlinked path
  components are refused;
* an existing run root is never deleted, reset, or overwritten;
* with ``--live-llm``/``--capture`` the provider is called only in attempt-1;
  attempt-2 reads the recordings attempt-1 wrote, so determinism is not merely
  provider agreement;
* a missing MANAGEMENT/REVIEW recording fails closed with a typed error that
  names the role and input hash; replay never invents a decision.

The live database, paper account, OMS ledger, and budget ledger are never
opened: each attempt builds its own container with its own ``data_dir``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from dataclasses import replace as replace_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
    write_atomic,
)
from oft.replay.paper_loop import (
    DEFAULT_REPLAY_ENVIRONMENT,
    DEFAULT_REPLAY_NOW,
    ReplayRecordingInvalidError,
    ReplayRecordingMissingError,
    validate_run_id,
)
from oft.replay.support import (
    PINNED_OUTCOME_SETTINGS,
    pinned_fixture_paths,
    reject_symlink_components,
)

if TYPE_CHECKING:
    from oft.bootstrap import AppContainer
    from oft.config import Settings
    from oft.ports.llm import LLMPort
    from oft.ports.store import ReportRecord

POSITION_MANAGEMENT_NAMESPACE = "position-management"
SCENARIO_VERSION = "1"
_READ_ONLY_ENVIRONMENTS = frozenset({"PAPER", "TEST"})
_ATTEMPT_1 = "attempt-1"
_ATTEMPT_2 = "attempt-2"
_FUNDING_RATE = "0.0001"
# Two settlements on the recorded 8-hour cadence, both inside the holding
# window, so the closed trade has no "expected but not recorded" funding gap.
_FUNDING_OFFSETS = (timedelta(hours=6), timedelta(hours=14))
_FUNDING_ACCRUAL_OFFSET = timedelta(hours=15)

# (action label, capture fixture, hours after the entry instant)
_MANAGEMENT_STEPS = (
    ("PARTIAL_CLOSE", "management_partial_close.json", timedelta(0)),
    ("ADD", "management_add.json", timedelta(hours=4)),
    ("HOLD", "management_hold.json", timedelta(hours=8)),
)
_FULL_CLOSE_STEP = ("FULL_CLOSE", "management_full_close.json", timedelta(hours=16))
# Recorded bars the scenario publishes: (hours after entry, close).
_RECORDED_BARS = (
    (4, "70100"),
    (8, "70200"),
    (16, "70300"),
)
_UNSTABLE_IDS = frozenset({"scan_id", "report_id"})
# Scenario idempotency keys are constants, not run-id-derived: the run id only
# names the output directory. A derived key would change the decision ids in
# the management history and therefore the prompt input hashes, so recordings
# captured under one run id could never satisfy a replay under another.
_ENTRY_KEY = "replay-position-management-entry"
_STEP_KEY_PREFIX = "replay-position-management"
_REVIEW_KEY = "replay-position-management-review"


class PositionManagementReplayError(ReplayError):
    """The position-management scenario could not complete deterministically."""


@dataclass(frozen=True)
class PositionManagementConfig:
    """Inputs that fully determine one position-management replay."""

    symbol: str
    archive_fixture_dir: Path
    backfill_start: date
    backfill_end: date
    recordings_dir: Path | None = None
    period: str = "4h"
    live_llm: bool = False
    capture_recordings: bool = False
    provider: str | None = None
    environment: str = DEFAULT_REPLAY_ENVIRONMENT
    now: datetime = DEFAULT_REPLAY_NOW
    run_id: str = "position-management"
    llm_fixtures_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.environment.strip().upper() not in _READ_ONLY_ENVIRONMENTS:
            raise ReplayLivePathError(
                f"replay refuses environment {self.environment!r}; "
                "only PAPER/TEST replay is supported and a live order path is never opened"
            )
        if self.now.tzinfo is None:
            raise ReplayError("position-management replay requires a timezone-aware fixed clock")
        if self.backfill_end < self.backfill_start:
            raise ReplayError("backfill_end must be on or after backfill_start")
        if self.period != "4h":
            raise PositionManagementReplayError(
                "the position-management scenario replays a fixed 4h bar cadence "
                f"(seeded bar span, funding settlements, and step clock); period must be "
                f"'4h', got {self.period!r}"
            )
        validate_run_id(self.run_id)
        if self.recordings_dir is None:
            raise ReplayError(
                "recordings_dir is required; pass --recordings-dir first, then populate it "
                "with --capture (offline stub) or --live-llm (explicit provider)."
            )


@dataclass(frozen=True)
class ManagementStepOutcome:
    """One stable projection of a management action the scenario executed."""

    action: str
    decision_id: str
    status: str
    executed: bool
    size_after: str
    order_id: str | None
    fill_id: str | None


@dataclass(frozen=True)
class PositionManagementOutcome:
    """The stable values one attempt produced (never a path or random id)."""

    entry_decision_id: str
    entry_trigger_id: str
    position_id: str
    management: tuple[ManagementStepOutcome, ...]
    funding: dict[str, Any]
    closed_position: dict[str, Any]
    trade_log: dict[str, Any]
    statistics: dict[str, Any]
    review: dict[str, Any]


@dataclass(frozen=True)
class PositionManagementResult:
    """The identity of one verified deterministic position-management replay."""

    run_id: str
    scenario: str
    position_id: str
    trade_id: str
    review_decision_id: str
    statistics_status: str
    data_quality: str
    summary_path: str
    deterministic: Literal[True] = True


def _stable_projection(value: Any) -> Any:
    """Drop run-generated scan/report ids; every other value must match exactly.

    A backfill run creates a fresh ``scan_id``/``report_id`` per attempt, so two
    independent stores can never share them. They are the only non-deterministic
    inputs the replay consumes; the decision ids, prompts, marks, PnL, and
    statistics derived from them are all deterministic.
    """
    if isinstance(value, Mapping):
        return {
            key: _stable_projection(item)
            for key, item in sorted(value.items())
            if key not in _UNSTABLE_IDS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_projection(item) for item in value]
    return value


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _recording_failure(code: str, message: str) -> ReplayError:
    """Translate a service rejection into the matching typed replay error."""
    if code == "LLM_RECORDING_MISSING":
        return ReplayRecordingMissingError(message)
    if code == "LLM_RECORDING_INVALID":
        return ReplayRecordingInvalidError(message)
    return PositionManagementReplayError(message)


class PositionManagementReplay:
    """Run the management/journal/review scenario twice and prove determinism."""

    def __init__(self, *, data_dir: Path, config: PositionManagementConfig) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE
        self._scenario_root = self._replay_root / POSITION_MANAGEMENT_NAMESPACE
        self._run_root = self._scenario_root / config.run_id
        reject_symlink_components(self._run_root, stop=self._replay_root)
        if not self._run_root.resolve().is_relative_to(self._scenario_root.resolve()):
            raise ReplayError(
                f"replay run {config.run_id!r} escapes {self._scenario_root}; pass a plain run id"
            )

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    @property
    def run_root(self) -> Path:
        return self._run_root

    # -- isolated container ------------------------------------------------

    def _settings(self, attempt_dir: Path) -> Settings:
        from oft.config import DEFAULT_LLM_FIXTURES_DIR, Settings

        values: dict[str, Any] = {
            "env": "test",
            "port": 8765,
            "data_dir": attempt_dir,
            "archive_fixture_dir": self._config.archive_fixture_dir,
            "llm_model": "fixture",
            "llm_fixtures_dir": self._config.llm_fixtures_dir or DEFAULT_LLM_FIXTURES_DIR,
        }
        values.update(pinned_fixture_paths())
        values.update(PINNED_OUTCOME_SETTINGS)
        if self._config.live_llm:
            # Honour the configured provider (explicit --provider wins), never
            # default to a live provider when none is named. Leaving the field
            # unset lets ``Settings`` resolve the documented OFT_LLM_PROVIDER.
            if self._config.provider is not None:
                values["llm_provider"] = self._config.provider
        else:
            values["llm_provider"] = "stub"
        return Settings(**values)

    def _attempt_dir(self, attempt: str) -> Path:
        raw = self.run_root / attempt
        reject_symlink_components(raw, stop=self.run_root)
        resolved = raw.resolve()
        if not resolved.is_relative_to(self._scenario_root.resolve()):
            raise ReplayError(f"replay {attempt} escaped the replay namespace")
        return resolved

    def _recordings_dir(self) -> Path:
        assert self._config.recordings_dir is not None
        return Path(self._config.recordings_dir).expanduser().resolve()

    def _source(
        self,
        container: AppContainer,
        settings: Settings,
        attempt: str,
        *,
        management_fixture: str | None = None,
    ) -> LLMPort:
        """Recording provider on capture attempt-1, recordings everywhere else."""
        from oft.adapters.llm_stub import LLMStub
        from oft.application.prompt_service import secret_values_from_settings
        from oft.ports.llm import LLMRole
        from oft.replay.llm_source import RecordedLLMSource, RecordingLLMSource

        if attempt != _ATTEMPT_1 or not (self._config.live_llm or self._config.capture_recordings):
            return RecordedLLMSource(recordings_dir=self._recordings_dir())
        if self._config.live_llm:
            inner = container.llm_provider
            if inner is None:
                raise ReplayError(
                    "no live LLM provider is configured; set OFT_LLM_PROVIDER or omit --live-llm"
                )
        else:
            overrides = (
                {LLMRole.MANAGEMENT: management_fixture} if management_fixture is not None else {}
            )
            inner = LLMStub(fixtures_dir=settings.llm_fixtures_dir, overrides=overrides)
        return RecordingLLMSource(
            inner=inner,
            recordings_dir=self._recordings_dir(),
            clock=container.clock,
            secrets=secret_values_from_settings(settings),
        )

    # -- recorded market data ---------------------------------------------

    @staticmethod
    def _bar_series(container: AppContainer) -> tuple[str, str]:
        """The period and metric version of the series the scan published."""
        for entry in container.store.list_manifest():
            if entry.data_type != "bars" or not entry.metric_version:
                continue
            marker = "/period="
            if marker in entry.path:
                return entry.path.split(marker, 1)[1].split("/", 1)[0], entry.metric_version
        raise PositionManagementReplayError("the scan did not publish a bars series")

    def _seed_bar(
        self,
        container: AppContainer,
        *,
        close: Decimal,
        bar_end: datetime,
        run_id: str,
    ) -> None:
        """Publish one recorded bar so the mark/funding resolvers see it."""
        from oft.adapters.parquet_store import ParquetStore
        from oft.domain.resampling import Bar
        from oft.domain.time import utc_timestamp

        period, metric_version = self._bar_series(container)
        bar_start = bar_end - timedelta(hours=4)
        bar = Bar(
            symbol=self._config.symbol,
            period=period,
            bar_start=bar_start,
            bar_end=bar_end,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("1"),
            quote_volume=close,
            trade_count=1,
            taker_buy_volume=Decimal("0"),
            source_mode="BACKFILL",
        )
        entry = ParquetStore(container.settings).write_bar_part(
            (bar,),
            symbol=self._config.symbol,
            period=period,
            window_start=bar_start,
            window_end=bar_end,
            metric_version=metric_version,
            input_manifest_hash=f"replay-bar-{run_id}",
            source_mode="BACKFILL",
            source_digest=f"replay-bar-{run_id}",
            run_id=run_id,
            computed_at=utc_timestamp(bar_end),
            data_quality="COMPLETE",
            quality_reasons=(),
        )
        container.store.upsert_manifest_entry(entry)

    def _seed_funding_report(
        self,
        container: AppContainer,
        *,
        trigger_id: str,
        settlements: tuple[datetime, ...],
    ) -> None:
        """Record funding settlements the accrual path can honestly consume."""
        from oft.domain.time import utc_timestamp
        from oft.ports.store import ReportRecord

        if not settlements:
            raise PositionManagementReplayError("a funding report needs a settlement")
        source = self._source_report(container, trigger_id)
        snapshot = json.loads(source.snapshot_json)
        if not isinstance(snapshot, dict):
            raise PositionManagementReplayError("the recorded source report snapshot is invalid")
        orderflow = snapshot.setdefault("orderflow", {})
        if not isinstance(orderflow, dict):
            raise PositionManagementReplayError("the recorded source report orderflow is invalid")
        orderflow["funding"] = {
            "current_rate": _FUNDING_RATE,
            "next_funding_time": utc_timestamp(settlements[0]),
            "interval_hours": 8,
            "mark_price": "70100",
            "index_price": "70100",
            "history": [
                {"ts": utc_timestamp(settlement), "rate": _FUNDING_RATE, "interval_hours": 8}
                for settlement in settlements
            ],
        }
        record = ReportRecord(
            report_id="RPT-REPLAY-FUNDING",
            scan_id=source.scan_id,
            symbol=source.symbol,
            timeframe=source.timeframe,
            generated_at=utc_timestamp(settlements[-1]),
            metrics_manifest_json=source.metrics_manifest_json,
            llm_decision_id=source.llm_decision_id,
            narrative_ref=source.narrative_ref,
            data_quality=source.data_quality,
            snapshot_json=json.dumps(snapshot, sort_keys=True),
            metrics_json=source.metrics_json,
            llm_json=source.llm_json,
            window_policy_json=source.window_policy_json,
        )
        container.store.save_report(record)

    @staticmethod
    def _source_report(container: AppContainer, trigger_id: str) -> ReportRecord:
        trigger = container.store.get_trigger_event(trigger_id)
        if trigger is None:
            raise PositionManagementReplayError("the replay trigger disappeared")
        opportunity = container.store.get_opportunity(trigger.opportunity_id)
        if opportunity is None:
            raise PositionManagementReplayError("the replay opportunity disappeared")
        payload = json.loads(opportunity.payload_json)
        report_id = str((payload.get("source") or {}).get("report_id") or "")
        report = container.store.get_report(report_id) if report_id else None
        if report is None:
            raise PositionManagementReplayError(
                "the replay opportunity has no recorded source report for funding"
            )
        return report

    # -- one attempt -------------------------------------------------------

    def _entry(
        self,
        container: AppContainer,
        settings: Settings,
        source: LLMPort,
    ) -> dict[str, Any]:
        from oft.application.entry_service import EntryService
        from oft.application.kill_switch import KillSwitch
        from oft.application.scan_service import ScanRequest, ScanService
        from oft.application.trigger_service import TriggerService

        provider_name = str(settings.llm_provider or "stub")
        backfill = container.backfill.run(
            symbol=self._config.symbol,
            data_type="aggTrades",
            start=self._config.backfill_start,
            end=self._config.backfill_end,
        )
        if backfill.run.status != "COMPLETE":
            raise PositionManagementReplayError(
                f"replay backfill did not complete: {backfill.run.status}"
            )
        ScanService(container=container, lock=container.scan_lock).run(
            ScanRequest(
                symbol=self._config.symbol,
                period=self._config.period,
                metrics_only=False,
                provider=provider_name,
            )
        )
        opportunities = container.store.list_opportunities(state="OPEN")
        if not opportunities:
            raise PositionManagementReplayError("replay produced no OPEN opportunity")
        trigger_run = TriggerService(store=container.store, clock=container.clock).evaluate(
            opportunities[0].opportunity_id
        )
        if trigger_run.trigger is None:
            raise PositionManagementReplayError(
                "replay trigger did not fire; the fixture window is not replayable: "
                + "; ".join(trigger_run.evaluation.reasons)
            )
        accepted = EntryService(
            store=container.store,
            clock=container.clock,
            provider=source,
            settings=settings,
            kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
        ).accept(
            trigger_run.trigger.trigger_id,
            idempotency_key=_ENTRY_KEY,
        )
        if accepted.position is None:
            raise PositionManagementReplayError("replay produced no paper position")
        return {
            "decision_id": accepted.decision_id,
            "trigger_id": trigger_run.trigger.trigger_id,
            "position": accepted.position,
        }

    def _manage_step(
        self,
        container: AppContainer,
        settings: Settings,
        attempt: str,
        *,
        action: str,
        fixture: str,
        at: datetime,
    ) -> tuple[Any, ManagementStepOutcome]:
        from oft.application.kill_switch import KillSwitch
        from oft.application.management_service import (
            ManagementRejected,
            ManagementService,
        )
        from oft.clock import FixedClock

        source = self._source(container, settings, attempt, management_fixture=fixture)
        clock = FixedClock(at)
        service = ManagementService(
            store=container.store,
            clock=clock,
            provider=source,
            settings=settings,
            kill_switch=KillSwitch(settings=settings, store=container.store, clock=clock),
            runtime=container.llm_runtime,
        )
        try:
            result = service.manage(
                self._position_id(container),
                idempotency_key=f"{_STEP_KEY_PREFIX}-{action.lower()}",
            )
        except ManagementRejected as exc:
            raise _recording_failure(exc.code, exc.message) from exc
        payload = self._position_payload(container)
        order = result.order or {}
        fill = result.fill or {}
        return result, ManagementStepOutcome(
            action=result.action,
            decision_id=result.decision_id,
            status=result.status,
            executed=result.executed,
            size_after=str(payload.get("size")),
            order_id=str(order.get("order_id")) if order.get("order_id") else None,
            fill_id=str(fill.get("fill_id")) if fill.get("fill_id") else None,
        )

    def _position_id(self, container: AppContainer) -> str:
        positions = container.store.list_positions(limit=None)
        if len(positions) != 1:
            raise PositionManagementReplayError(
                f"replay must produce exactly one paper position, produced {len(positions)}"
            )
        return positions[0].position_id

    def _position_payload(self, container: AppContainer) -> dict[str, Any]:
        record = container.store.get_position(self._position_id(container))
        if record is None:
            raise PositionManagementReplayError("replay position disappeared")
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise PositionManagementReplayError("replay position payload is not an object")
        return payload

    def _review(
        self,
        container: AppContainer,
        settings: Settings,
        attempt: str,
        *,
        trade_id: str,
        at: datetime,
    ) -> dict[str, Any]:
        from oft.application.review_models import ReviewRejected
        from oft.application.review_service import ReviewService
        from oft.clock import FixedClock

        source = self._source(container, settings, attempt)
        clock = FixedClock(at)
        service = ReviewService(
            store=container.store,
            clock=clock,
            settings=settings,
            provider=source,
            runtime=container.llm_runtime,
        )
        try:
            result = service.review(trade_id, idempotency_key=_REVIEW_KEY)
        except ReviewRejected as exc:
            raise _recording_failure(exc.code, exc.message) from exc
        decision = container.store.get_decision(result.decision_id)
        if decision is None:
            raise PositionManagementReplayError("replay review decision disappeared")
        envelope = json.loads(decision.payload_json)
        if not isinstance(envelope, dict):
            raise PositionManagementReplayError("replay review envelope is not an object")
        return {
            "decision_id": result.decision_id,
            "status": str(envelope.get("validation_status") or ""),
            "envelope": envelope,
        }

    def _run_once(self, attempt: str) -> PositionManagementOutcome:
        from oft.application.journal_service import JournalService
        from oft.application.position_service import PositionService
        from oft.bootstrap import build_container
        from oft.clock import FixedClock
        from oft.domain.time import utc_timestamp

        attempt_dir = self._attempt_dir(attempt)
        settings = self._settings(attempt_dir)
        clock = FixedClock(self._config.now)
        container = build_container(settings, initialize=True, clock=clock)
        entry_source = self._source(container, settings, attempt)
        container = replace_dataclass(container, llm_provider=entry_source)
        entry = self._entry(container, settings, entry_source)
        position_id = str(entry["position"]["position_id"])

        # Publish the recorded bars the management/funding resolvers consume.
        for offset_hours, close in _RECORDED_BARS:
            self._seed_bar(
                container,
                close=Decimal(close),
                bar_end=self._config.now + timedelta(hours=offset_hours),
                run_id=f"replay-bar-{offset_hours}",
            )
        self._seed_funding_report(
            container,
            trigger_id=str(entry["trigger_id"]),
            settlements=tuple(self._config.now + offset for offset in _FUNDING_OFFSETS),
        )

        management: list[ManagementStepOutcome] = []
        for action, fixture, offset in _MANAGEMENT_STEPS:
            _result, step = self._manage_step(
                container,
                settings,
                attempt,
                action=action,
                fixture=fixture,
                at=self._config.now + offset,
            )
            management.append(step)

        accrual_at = self._config.now + _FUNDING_ACCRUAL_OFFSET
        funding = PositionService(
            store=container.store, clock=FixedClock(accrual_at), settings=settings
        ).accrue_funding(position_id, now=utc_timestamp(accrual_at))

        action, fixture, offset = _FULL_CLOSE_STEP
        close_at = self._config.now + offset
        _close_result, close_step = self._manage_step(
            container,
            settings,
            attempt,
            action=action,
            fixture=fixture,
            at=close_at,
        )
        management.append(close_step)

        trades = container.store.list_trade_logs(limit=None)
        if len(trades) != 1:
            raise PositionManagementReplayError(
                f"replay must produce exactly one trade log, produced {len(trades)}"
            )
        trade_id = trades[0].trade_id
        review = self._review(
            container,
            settings,
            attempt,
            trade_id=trade_id,
            at=close_at,
        )
        # Read the trade log after the review link so the stable projection
        # carries the advisory ``review_decision_id`` the journal renders.
        linked = container.store.get_trade_log(trade_id)
        if linked is None:
            raise PositionManagementReplayError("replay trade log disappeared after review")
        trade = json.loads(linked.payload_json)
        if not isinstance(trade, dict):
            raise PositionManagementReplayError("replay trade log is not an object")
        closed_position = self._position_payload(container)

        journal = JournalService(
            store=container.store, clock=FixedClock(close_at), settings=settings
        )
        statistics = journal.stats(symbol=self._config.symbol)
        return PositionManagementOutcome(
            entry_decision_id=str(entry["decision_id"]),
            entry_trigger_id=str(entry["trigger_id"]),
            position_id=position_id,
            management=tuple(management),
            funding=dict(funding),
            closed_position=closed_position,
            trade_log=trade,
            statistics=statistics,
            review=review,
        )

    # -- public entry point ------------------------------------------------

    def run(self) -> PositionManagementResult:
        """Run the scenario twice, compare every stable value, and write a summary."""
        reject_symlink_components(self._run_root, stop=self._replay_root)
        if self._run_root.exists():
            raise ReplayError(
                f"run '{self._config.run_id}' already exists at {self._run_root}; "
                "pass a new --run-id (replay never deletes or overwrites a run)"
            )

        first = self._run_once(_ATTEMPT_1)
        second = self._run_once(_ATTEMPT_2)
        left = _stable_projection(asdict(first))
        right = _stable_projection(asdict(second))
        if _canonical_json(left) != _canonical_json(right):
            raise ReplayNonDeterministicError(
                "position-management replay is non-deterministic: attempt-1 and attempt-2 "
                "produced different management actions, funding, PnL, statistics, or reviews"
            )

        summary_path = (self.run_root / "summary.json").resolve()
        if not summary_path.is_relative_to(self._replay_root.resolve()):
            raise ReplayError("replay summary escaped the replay namespace")
        summary = {
            "scenario": POSITION_MANAGEMENT_NAMESPACE,
            "scenario_version": SCENARIO_VERSION,
            "run_id": self._config.run_id,
            "environment": self._config.environment.upper(),
            "symbol": self._config.symbol,
            "period": self._config.period,
            "fixed_clock": self._config.now.astimezone(UTC).isoformat(),
            "attempts": 2,
            "deterministic": True,
            "outcome": left,
        }
        write_atomic(summary_path, _canonical_json(summary))

        trade_quality = str((first.trade_log.get("data_quality") or {}).get("status") or "PARTIAL")
        return PositionManagementResult(
            run_id=self._config.run_id,
            scenario=POSITION_MANAGEMENT_NAMESPACE,
            position_id=first.position_id,
            trade_id=str(first.trade_log.get("trade_id") or ""),
            review_decision_id=str(first.review.get("decision_id") or ""),
            statistics_status=str(first.statistics.get("status") or ""),
            data_quality=trade_quality,
            summary_path=str(summary_path),
        )


__all__ = [
    "POSITION_MANAGEMENT_NAMESPACE",
    "SCENARIO_VERSION",
    "ManagementStepOutcome",
    "PositionManagementConfig",
    "PositionManagementOutcome",
    "PositionManagementReplay",
    "PositionManagementReplayError",
    "PositionManagementResult",
]
