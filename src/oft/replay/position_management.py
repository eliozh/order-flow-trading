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
from dataclasses import dataclass
from dataclasses import replace as replace_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from oft.config import Environment
from oft.domain.risk import ManagementAction, ManagementStatus
from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
    write_atomic,
)
from oft.replay.llm_source import RecordingErrorCode
from oft.replay.paper_loop import DEFAULT_REPLAY_NOW, validate_run_id
from oft.replay.support import (
    READ_ONLY_ENVIRONMENTS,
    ReplayRecordingInvalidError,
    ReplayRecordingMissingError,
    build_replay_settings,
    canonical_json,
    guard_new_run_root,
    guard_replay_run_root,
    project_stable_ids,
    raise_no_replay_opportunity,
    replay_identity_digest,
    resolve_attempt_dir,
    resolve_recordings_dir,
    resolve_summary_path,
)

if TYPE_CHECKING:
    from oft.bootstrap import AppContainer
    from oft.config import Settings
    from oft.ports.llm import LLMPort
    from oft.ports.store import ReportRecord

POSITION_MANAGEMENT_NAMESPACE = "position-management"
SCENARIO_VERSION = "1"
_ATTEMPT_1 = "attempt-1"
_ATTEMPT_2 = "attempt-2"
_FUNDING_RATE = "0.0001"
# Two settlements on the recorded 8-hour cadence, both inside the holding
# window, so the closed trade has no "expected but not recorded" funding gap.
_FUNDING_OFFSETS = (timedelta(hours=6), timedelta(hours=14))
_FUNDING_ACCRUAL_OFFSET = timedelta(hours=15)


@dataclass(frozen=True)
class _ManagementStepSpec:
    """The exact action/outcome one recorded management step must produce."""

    action: ManagementAction
    fixture: str
    offset: timedelta
    expected_status: ManagementStatus
    expected_executed: bool


# (requested action, capture fixture, hours after the entry instant, outcome)
_MANAGEMENT_STEPS = (
    _ManagementStepSpec(
        ManagementAction.PARTIAL_CLOSE,
        "management_partial_close.json",
        timedelta(0),
        ManagementStatus.EXECUTED,
        True,
    ),
    _ManagementStepSpec(
        ManagementAction.ADD,
        "management_add.json",
        timedelta(hours=4),
        ManagementStatus.EXECUTED,
        True,
    ),
    _ManagementStepSpec(
        ManagementAction.HOLD,
        "management_hold.json",
        timedelta(hours=8),
        ManagementStatus.NO_ACTION,
        False,
    ),
)
_FULL_CLOSE_STEP = _ManagementStepSpec(
    ManagementAction.FULL_CLOSE,
    "management_full_close.json",
    timedelta(hours=16),
    ManagementStatus.EXECUTED,
    True,
)
# Recorded bars the scenario publishes: (hours after entry, close).
_RECORDED_BARS = (
    (4, "70100"),
    (8, "70200"),
    (16, "70300"),
)
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
    recordings_dir: Path
    period: Literal["4h"] = "4h"
    live_llm: bool = False
    capture_recordings: bool = False
    force_recordings: bool = False
    provider: str | None = None
    environment: Environment = Environment.PAPER
    now: datetime = DEFAULT_REPLAY_NOW
    run_id: str = "position-management"
    llm_fixtures_dir: Path | None = None

    def __post_init__(self) -> None:
        environment = Environment(self.environment.strip().lower())
        if environment not in READ_ONLY_ENVIRONMENTS:
            raise ReplayLivePathError(
                f"replay refuses environment {self.environment!r}; "
                "only PAPER/TEST replay is supported and a live order path is never opened"
            )
        object.__setattr__(self, "environment", environment)
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


@dataclass(frozen=True)
class ManagementStepExecution:
    """The order/fill a management action executed; both ids are required."""

    order_id: str
    fill_id: str

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise PositionManagementReplayError(
                "management execution is missing a non-empty order_id"
            )
        if not self.fill_id.strip():
            raise PositionManagementReplayError(
                "management execution is missing a non-empty fill_id"
            )


@dataclass(frozen=True)
class ManagementStepOutcome:
    """One stable projection of a management action the scenario executed.

    ``execution`` is a union: an ``EXECUTED`` status requires order and fill
    ids, and any other status forbids them. The impossible
    ``executed=True, order_id=None`` state cannot be represented.
    """

    action: ManagementAction
    decision_id: str
    status: ManagementStatus
    execution: ManagementStepExecution | None
    size_after: str

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise PositionManagementReplayError(
                "management step is missing a non-empty decision_id"
            )
        if not self.size_after.strip():
            raise PositionManagementReplayError("management step is missing a non-empty size_after")
        executed = self.status is ManagementStatus.EXECUTED
        if executed != (self.execution is not None):
            raise PositionManagementReplayError(
                f"management step {self.action.value} has status {self.status.value} "
                "but its execution does not match"
            )

    def to_payload(self) -> dict[str, Any]:
        """The flat JSON shape the summary and determinism comparison use."""
        execution = self.execution
        return {
            "action": self.action.value,
            "decision_id": self.decision_id,
            "status": self.status.value,
            "executed": execution is not None,
            "size_after": self.size_after,
            "order_id": execution.order_id if execution is not None else None,
            "fill_id": execution.fill_id if execution is not None else None,
        }


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

    def to_payload(self) -> dict[str, Any]:
        """An explicit projection of every stable value one attempt produced.

        The only run-generated ids are the source ``scan_id``/``report_id``
        copied into the trade-log signal and the review envelope subject; they
        are projected out by name through the shared helper. No blanket
        key-name recursion is applied to unrelated numbers or ids.
        """
        trade_log = json.loads(json.dumps(self.trade_log))
        signal = trade_log.get("signal")
        if isinstance(signal, dict):
            trade_log["signal"] = project_stable_ids(signal)
        review = json.loads(json.dumps(self.review))
        envelope = review.get("envelope")
        if isinstance(envelope, dict):
            subject = envelope.get("subject")
            if isinstance(subject, dict):
                envelope["subject"] = project_stable_ids(subject)
        return {
            "entry_decision_id": self.entry_decision_id,
            "entry_trigger_id": self.entry_trigger_id,
            "position_id": self.position_id,
            "management": [step.to_payload() for step in self.management],
            "funding": self.funding,
            "closed_position": self.closed_position,
            "trade_log": trade_log,
            "statistics": self.statistics,
            "review": review,
        }


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


@dataclass(frozen=True)
class PositionManagementSummary:
    """The persisted, typed summary of one verified replay.

    ``identity_digest`` covers every fixture and pinned setting that can change
    the outcome, so two summaries over different inputs are distinguishable
    even when their stable outcome happens to agree.
    """

    scenario: str
    scenario_version: str
    run_id: str
    environment: str
    symbol: str
    period: str
    fixed_clock: str
    attempts: int
    identity_digest: str
    outcome: dict[str, Any]
    deterministic: Literal[True] = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "scenario_version": self.scenario_version,
            "run_id": self.run_id,
            "environment": self.environment,
            "symbol": self.symbol,
            "period": self.period,
            "fixed_clock": self.fixed_clock,
            "attempts": self.attempts,
            "deterministic": self.deterministic,
            "identity_digest": self.identity_digest,
            "outcome": self.outcome,
        }


def _recording_failure(code: str, message: str) -> ReplayError:
    """Translate a service rejection into the matching typed replay error."""
    if code == RecordingErrorCode.MISSING:
        return ReplayRecordingMissingError(message)
    if code == RecordingErrorCode.INVALID:
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
        guard_replay_run_root(
            run_root=self._run_root,
            replay_root=self._replay_root,
            scenario_root=self._scenario_root,
            run_id=config.run_id,
        )

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    @property
    def run_root(self) -> Path:
        return self._run_root

    # -- isolated container ------------------------------------------------

    def _settings(self, attempt_dir: Path) -> Settings:
        return build_replay_settings(
            attempt_dir=attempt_dir,
            archive_fixture_dir=self._config.archive_fixture_dir,
            llm_fixtures_dir=self._config.llm_fixtures_dir,
            live_llm=self._config.live_llm,
            provider=self._config.provider,
        )

    def _attempt_dir(self, attempt: str) -> Path:
        return resolve_attempt_dir(
            run_root=self.run_root,
            namespace_root=self._scenario_root,
            attempt=attempt,
        )

    def _recordings_dir(self) -> Path:
        return resolve_recordings_dir(self._config.recordings_dir)

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
        inner: LLMPort
        if self._config.live_llm:
            configured = str(settings.llm_provider or "").strip().lower()
            if management_fixture is not None and configured == "stub":
                # The offline stub returns one fixture per role; the scenario
                # needs four distinct recorded actions, so apply the same
                # per-step override the capture path uses. (The container's
                # provider may already be the entry recording wrapper here.)
                inner = LLMStub(
                    fixtures_dir=settings.llm_fixtures_dir,
                    overrides={LLMRole.MANAGEMENT: management_fixture},
                )
            else:
                live_provider = container.llm_provider
                if live_provider is None:
                    raise ReplayError(
                        "no live LLM provider is configured; set OFT_LLM_PROVIDER "
                        "or omit --live-llm"
                    )
                inner = live_provider
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
            force=self._config.force_recordings,
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
            raise_no_replay_opportunity(scan)
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
        spec: _ManagementStepSpec,
    ) -> ManagementStepOutcome:
        """Run one recorded management action and prove it matches the spec.

        A mismatched recording set (a different action, status, or execution) is
        a replay failure, never a "successful" run that exercised fewer actions.
        """
        from oft.application.kill_switch import KillSwitch
        from oft.application.management_service import (
            ManagementRejected,
            ManagementService,
        )
        from oft.application.position_service import PositionCloseError
        from oft.clock import FixedClock

        at = self._config.now + spec.offset
        source = self._source(container, settings, attempt, management_fixture=spec.fixture)
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
                idempotency_key=f"{_STEP_KEY_PREFIX}-{spec.action.value.lower()}",
            )
        except ManagementRejected as exc:
            raise _recording_failure(exc.code, exc.message) from exc
        except PositionCloseError as exc:
            # A partial close that escalated to a full close leaves the position
            # in a state the next step cannot use; surface the typed code.
            raise PositionManagementReplayError(
                f"management {spec.action.value} failed with {exc.code}: {exc.message}"
            ) from exc

        actual_action = ManagementAction(result.action)
        actual_status = ManagementStatus(result.status)
        if actual_action is not spec.action:
            raise PositionManagementReplayError(
                f"management step {spec.action.value} returned action "
                f"{actual_action.value}; the recording set does not match the scenario"
            )
        if actual_status is not spec.expected_status:
            raise PositionManagementReplayError(
                f"management step {spec.action.value} returned status "
                f"{actual_status.value}; expected {spec.expected_status.value}"
            )
        if bool(result.executed) != spec.expected_executed:
            raise PositionManagementReplayError(
                f"management step {spec.action.value} returned executed="
                f"{bool(result.executed)}; expected {spec.expected_executed}"
            )

        payload = self._position_payload(container)
        size_after = str(payload.get("size") or "").strip()
        if not size_after:
            raise PositionManagementReplayError(
                f"management step {spec.action.value} left no position size"
            )
        order = result.order or {}
        fill = result.fill or {}
        if spec.expected_executed:
            order_id = str(order.get("order_id") or "").strip()
            fill_id = str(fill.get("fill_id") or "").strip()
            if not order_id or not fill_id:
                raise PositionManagementReplayError(
                    f"management step {spec.action.value} executed without an order/fill id"
                )
            execution: ManagementStepExecution | None = ManagementStepExecution(
                order_id=order_id, fill_id=fill_id
            )
        else:
            if order or fill:
                raise PositionManagementReplayError(
                    f"management step {spec.action.value} produced an order/fill without executing"
                )
            execution = None
        return ManagementStepOutcome(
            action=actual_action,
            decision_id=result.decision_id,
            status=actual_status,
            execution=execution,
            size_after=size_after,
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
        for spec in _MANAGEMENT_STEPS:
            management.append(self._manage_step(container, settings, attempt, spec=spec))

        accrual_at = self._config.now + _FUNDING_ACCRUAL_OFFSET
        funding = PositionService(
            store=container.store, clock=FixedClock(accrual_at), settings=settings
        ).accrue_funding(position_id, now=utc_timestamp(accrual_at))
        self._validate_funding(funding)

        management.append(self._manage_step(container, settings, attempt, spec=_FULL_CLOSE_STEP))
        close_at = self._config.now + _FULL_CLOSE_STEP.offset

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
        self._validate_trade_funding(trade)
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

    # -- validation helpers ------------------------------------------------

    @staticmethod
    def _validate_funding(funding: Mapping[str, Any]) -> None:
        """Fail when seeded settlements did not resolve to real accruals (H5)."""
        reasons = tuple(funding.get("reasons") or ())
        accrued = funding.get("accrued")
        expected = len(_FUNDING_OFFSETS)
        if reasons or accrued != expected:
            raise PositionManagementReplayError(
                "replay funding accrual is degraded: "
                f"accrued={accrued!r} of {expected}, reasons={reasons!r}"
            )

    @staticmethod
    def _validate_trade_funding(trade: Mapping[str, Any]) -> None:
        """The closed trade must carry a parsed, non-zero recorded funding term."""
        raw = trade.get("funding_usd")
        try:
            funding_usd = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PositionManagementReplayError(
                f"replay trade log has an unreadable funding_usd: {raw!r}"
            ) from exc
        if not funding_usd.is_finite() or funding_usd == 0:
            raise PositionManagementReplayError(
                f"replay trade log funding_usd is zero or non-finite: {raw!r}"
            )

    @staticmethod
    def _require_identity(value: Any, field: str) -> str:
        """An identity field may never be absent or empty in a success summary."""
        text = str(value or "").strip()
        if not text:
            raise PositionManagementReplayError(f"replay result is missing a non-empty {field}")
        return text

    def _identity_digest(self) -> str:
        """A digest of every input that can change the replay outcome (M11)."""
        return replay_identity_digest(
            scenario=POSITION_MANAGEMENT_NAMESPACE,
            scenario_version=SCENARIO_VERSION,
            symbol=self._config.symbol,
            period=self._config.period,
            environment=self._config.environment.upper(),
            archive_fixture_dir=self._config.archive_fixture_dir,
            llm_fixtures_dir=self._config.llm_fixtures_dir,
            recordings_dir=self._recordings_dir(),
            live_llm=self._config.live_llm,
            provider=self._config.provider,
        )

    # -- public entry point ------------------------------------------------

    def run(self) -> PositionManagementResult:
        """Run the scenario twice, compare every stable value, and write a summary."""
        guard_new_run_root(
            run_root=self._run_root,
            replay_root=self._replay_root,
            run_id=self._config.run_id,
        )

        first = self._run_once(_ATTEMPT_1)
        second = self._run_once(_ATTEMPT_2)
        left = first.to_payload()
        right = second.to_payload()
        if canonical_json(left) != canonical_json(right):
            raise ReplayNonDeterministicError(
                "position-management replay is non-deterministic: attempt-1 and attempt-2 "
                "produced different management actions, funding, PnL, statistics, or reviews"
            )

        summary_path = resolve_summary_path(
            run_root=self.run_root,
            replay_root=self._replay_root,
        )

        # Validate every identity field and build the frozen result *before* the
        # summary: a run with an empty id must not leave a success-looking
        # ``"deterministic": true`` artifact behind (H3/M9).
        data_quality = first.trade_log.get("data_quality")
        if not isinstance(data_quality, Mapping):
            raise PositionManagementReplayError(
                "replay trade log is missing its data_quality object"
            )
        self._require_identity(first.entry_decision_id, "entry_decision_id")
        self._require_identity(first.entry_trigger_id, "entry_trigger_id")
        result = PositionManagementResult(
            run_id=self._config.run_id,
            scenario=POSITION_MANAGEMENT_NAMESPACE,
            position_id=self._require_identity(first.position_id, "position_id"),
            trade_id=self._require_identity(first.trade_log.get("trade_id"), "trade_id"),
            review_decision_id=self._require_identity(
                first.review.get("decision_id"), "review_decision_id"
            ),
            statistics_status=self._require_identity(
                first.statistics.get("status"), "statistics_status"
            ),
            data_quality=self._require_identity(
                data_quality.get("status"), "trade log data_quality.status"
            ),
            summary_path=str(summary_path),
        )

        summary = PositionManagementSummary(
            scenario=POSITION_MANAGEMENT_NAMESPACE,
            scenario_version=SCENARIO_VERSION,
            run_id=self._config.run_id,
            environment=self._config.environment.upper(),
            symbol=self._config.symbol,
            period=str(self._config.period),
            fixed_clock=self._config.now.astimezone(UTC).isoformat(),
            attempts=2,
            identity_digest=self._identity_digest(),
            outcome=left,
        )
        write_atomic(summary_path, canonical_json(summary.to_payload()))
        return result


__all__ = [
    "POSITION_MANAGEMENT_NAMESPACE",
    "SCENARIO_VERSION",
    "ManagementStepExecution",
    "ManagementStepOutcome",
    "PositionManagementConfig",
    "PositionManagementOutcome",
    "PositionManagementReplay",
    "PositionManagementReplayError",
    "PositionManagementResult",
    "PositionManagementSummary",
]
