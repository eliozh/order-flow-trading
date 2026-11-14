"""Deterministic paper-loop replay.

The metric harness in :mod:`oft.replay.harness` replays one metrics window.
This module replays the *full* deterministic path (backfill -> scan -> trigger
-> entry re-check -> risk -> paper fill) twice in two fresh stores that both
live under ``$OFT_DATA/replay/paper-loop/<run_id>/``. The live database, paper
account, OMS ledger, and budget ledger are never opened or written.

Safety rules:

* a run id is validated before use and the resolved run root must stay under
  ``$OFT_DATA/replay/paper-loop``; symlinked path components are refused;
* an existing run root is never deleted, reset, or overwritten -- ``run()``
  fails fast before any write and tells the operator to pass a new ``--run-id``;
* with ``--live-llm``/``--capture`` the provider is called only in attempt-1;
  attempt-2 reads the recordings attempt-1 wrote, so both attempts consume
  identical text and determinism is not merely provider agreement.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from dataclasses import replace as replace_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from oft.config import Environment
from oft.domain.trigger import CONTRACT_DATA_QUALITIES
from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
    write_atomic,
)
from oft.replay.support import (
    READ_ONLY_ENVIRONMENTS,
    ReplayRecordingError,
    ReplayRecordingInvalidError,
    ReplayRecordingMissingError,
    build_replay_settings,
    canonical_json,
    guard_new_run_root,
    guard_replay_run_root,
    project_stable_opportunity_source,
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
    from oft.ports.store import DecisionRecord

PAPER_LOOP_NAMESPACE = "paper-loop"
SCENARIO_VERSION = "1"
DEFAULT_REPLAY_ENVIRONMENT = "PAPER"
DEFAULT_REPLAY_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
# At least one alphanumeric is required, so "." and ".." are never run ids.
_RUN_ID_PATTERN = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9._-]{1,64}$")
_RUN_ID_RULE = (
    "run_id must contain at least one letter or digit and match "
    "^[A-Za-z0-9._-]{1,64}$ ('.', '..', and path segments are refused)"
)
_ATTEMPT_1 = "attempt-1"
_ATTEMPT_2 = "attempt-2"


def validate_run_id(run_id: str) -> str:
    """Reject ``.``, ``..``, and path-like ids before they reach the filesystem."""
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ReplayError(_RUN_ID_RULE)
    return run_id


@dataclass(frozen=True)
class PaperLoopConfig:
    """Inputs that fully determine one paper-loop replay."""

    symbol: str
    archive_fixture_dir: Path
    backfill_start: date
    backfill_end: date
    recordings_dir: Path
    period: str = "4h"
    live_llm: bool = False
    capture_recordings: bool = False
    force_recordings: bool = False
    provider: str | None = None
    environment: Environment = Environment.PAPER
    now: datetime = DEFAULT_REPLAY_NOW
    run_id: str = "paper-loop"
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
            raise ReplayError("paper-loop replay requires a timezone-aware fixed clock")
        if self.backfill_end < self.backfill_start:
            raise ReplayError("backfill_end must be on or after backfill_start")
        validate_run_id(self.run_id)


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
class PaperOrderSummary:
    """A paper order the replay produced; the id is required and non-empty."""

    order_id: str
    status: str
    client_order_id: str

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ReplayError("paper order is missing a non-empty order_id")


@dataclass(frozen=True)
class PaperFillSummary:
    """A paper fill the replay produced; the id is required and non-empty."""

    fill_id: str
    price: str
    fee: str
    source: str

    def __post_init__(self) -> None:
        if not self.fill_id.strip():
            raise ReplayError("paper fill is missing a non-empty fill_id")


@dataclass(frozen=True)
class PaperPositionSummary:
    """A paper position the replay produced; the id is required and non-empty."""

    position_id: str
    size: str
    notional_usd: str

    def __post_init__(self) -> None:
        if not self.position_id.strip():
            raise ReplayError("paper position is missing a non-empty position_id")


@dataclass(frozen=True)
class PaperLoopResult:
    """The identity of one verified deterministic paper-loop replay.

    The order, fill, and position are value objects with required ids, so an
    impossible "order without an order id" state cannot be represented, and
    ``deterministic`` can only be ``True`` because the result is built only
    after both attempts compared equal.
    """

    run_id: str
    trigger_id: str
    decision_id: str
    decision_validation: str
    data_quality: str
    order: PaperOrderSummary
    fill: PaperFillSummary
    position: PaperPositionSummary
    summary_path: str
    deterministic: Literal[True] = True


def _stable_opportunity(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the two ids a scan generates randomly; everything else is pinned."""
    return project_stable_opportunity_source(payload)


def _stable_outcome(outcome: PaperLoopOutcome) -> dict[str, Any]:
    """The projection two replay attempts must agree on exactly.

    Built from ``asdict`` so a new field is compared automatically; only the
    opportunity needs a stable projection because a scan generates two random
    ids.
    """
    stable = asdict(outcome)
    stable["opportunity"] = _stable_opportunity(outcome.opportunity)
    return stable


def _decision_payload(decision: DecisionRecord | None) -> dict[str, Any]:
    """Read the accepted decision; a missing or unvalidated decision is fatal."""
    if decision is None:
        raise ReplayError("replay decision disappeared after accept")
    payload = json.loads(decision.payload_json)
    if not isinstance(payload, dict):
        raise ReplayError("replay decision payload is not a JSON object")
    if not str(payload.get("validation_status") or "").strip():
        raise ReplayError("replay decision has an empty validation_status")
    return payload


def _trigger_data_quality(trigger_payload: Mapping[str, Any]) -> str:
    """Read the frozen snapshot's ``data_quality``; absent/invalid is fatal.

    The value lives at ``trigger_payload["snapshot"]["data_quality"]`` (the
    trigger event contract), not at the payload top level. Replay must never
    pass an empty or non-contract label through to the response envelope.
    """
    snapshot = trigger_payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ReplayError("replay trigger payload has no snapshot object")
    value = snapshot.get("data_quality")
    if not isinstance(value, str) or value not in CONTRACT_DATA_QUALITIES:
        raise ReplayError(
            "replay trigger snapshot data_quality is missing or invalid: "
            f"{value!r}; expected one of {sorted(CONTRACT_DATA_QUALITIES)}"
        )
    return value


class PaperLoopReplay:
    """Run the paper loop twice under replay and prove it is deterministic."""

    def __init__(self, *, data_dir: Path, config: PaperLoopConfig) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE
        self._paper_loop_root = self._replay_root / PAPER_LOOP_NAMESPACE
        self._run_root = self._paper_loop_root / config.run_id
        guard_replay_run_root(
            run_root=self._run_root,
            replay_root=self._replay_root,
            scenario_root=self._paper_loop_root,
            run_id=config.run_id,
        )

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    @property
    def run_root(self) -> Path:
        return self._run_root

    def _settings(self, attempt_dir: Path) -> Settings:
        return build_replay_settings(
            attempt_dir=attempt_dir,
            archive_fixture_dir=self._config.archive_fixture_dir,
            llm_fixtures_dir=self._config.llm_fixtures_dir,
            live_llm=self._config.live_llm,
            provider=self._config.provider,
        )

    def _identity_digest(self) -> str:
        """A stable, checkout-independent digest of the inputs behind a replay.

        Fixtures are hashed by content, not by absolute path: two checkouts with
        byte-identical fixtures produce the same digest. The shared builder keeps
        this digest and the position-management identity digest consistent.
        """
        return replay_identity_digest(
            scenario=PAPER_LOOP_NAMESPACE,
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

    def _recordings_dir(self) -> Path:
        return resolve_recordings_dir(self._config.recordings_dir)

    def _source(
        self,
        container: AppContainer,
        settings: Settings,
        attempt: str,
    ) -> LLMPort:
        from oft.adapters.llm_stub import LLMStub
        from oft.application.prompt_service import secret_values_from_settings
        from oft.replay.llm_source import RecordedLLMSource, RecordingLLMSource

        if attempt != _ATTEMPT_1 or not (self._config.live_llm or self._config.capture_recordings):
            # Read-only replay, or attempt-2 of a capture: both consume exactly
            # what attempt-1 recorded. The provider is never called twice.
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
            force=self._config.force_recordings,
        )

    @staticmethod
    def _provider_name(source: LLMPort) -> str:
        return str(getattr(source, "provider", "") or "stub")

    def _attempt_dir(self, attempt: str) -> Path:
        """Resolve one attempt directory and prove it stays in the namespace."""
        return resolve_attempt_dir(
            run_root=self.run_root,
            namespace_root=self._paper_loop_root,
            attempt=attempt,
        )

    def _run_once(self, attempt: str) -> PaperLoopOutcome:
        from oft.application.entry_service import EntryService
        from oft.application.kill_switch import KillSwitch
        from oft.application.scan_service import ScanRequest, ScanService
        from oft.application.trigger_service import TriggerService
        from oft.bootstrap import build_container
        from oft.clock import FixedClock

        attempt_dir = self._attempt_dir(attempt)
        settings = self._settings(attempt_dir)
        clock = FixedClock(self._config.now)
        container = build_container(settings, initialize=True, clock=clock)
        source = self._source(container, settings, attempt)
        container = replace_dataclass(container, llm_provider=source)
        # The configured name is what ``container.provider_for`` compares
        # against, so both attempts route through the replaced source even when
        # a live provider name is configured.
        provider_name = str(settings.llm_provider or self._provider_name(source))

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
            raise_no_replay_opportunity(scan)
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
        decision_payload = _decision_payload(container.store.get_decision(result.decision_id))
        provenance = decision_payload.get("provenance") or {}
        metrics_payload = json.loads(scan.report.metrics_json)

        return PaperLoopOutcome(
            decision_id=result.decision_id,
            decision_validation=str(decision_payload["validation_status"]),
            metric_version=metrics_payload.get("metric_version"),
            prompt_version=provenance.get("prompt_version"),
            knowledge_artifact_hash=provenance.get("knowledge_artifact_hash"),
            llm_provider=provenance.get("provider"),
            llm_model=provenance.get("model"),
            data_quality=_trigger_data_quality(trigger_payload),
            opportunity=json.loads(opportunity.payload_json),
            trigger=trigger_payload,
            order=result.order,
            fill=result.fill,
            position=result.position,
            account=result.account,
            risk_verdict=result.risk_verdict,
        )

    def run(self) -> PaperLoopResult:
        """Run the loop twice, compare every stable value, and write a summary.

        An existing run root fails closed before any write: replay never
        deletes, resets, or overwrites a previous run. Pass a new ``--run-id``.
        """
        guard_new_run_root(
            run_root=self._run_root,
            replay_root=self._replay_root,
            run_id=self._config.run_id,
        )

        first = self._run_once(_ATTEMPT_1)
        second = self._run_once(_ATTEMPT_2)
        left = _stable_outcome(first)
        right = _stable_outcome(second)
        if canonical_json(left) != canonical_json(right):
            raise ReplayNonDeterministicError(
                "paper-loop replay is non-deterministic: attempt-1 and attempt-2 produced "
                "different decisions, triggers, orders, fills, positions, or account values"
            )

        summary_path = resolve_summary_path(
            run_root=self.run_root,
            replay_root=self._replay_root,
        )

        # Validate the artifacts and build the value object before writing the
        # summary: a run that cannot produce an order, fill, or position (or a
        # value object with an empty id) must not leave a success-looking
        # ``"deterministic": true`` artifact behind.
        order = first.order
        fill = first.fill
        position = first.position
        if order is None:
            raise ReplayError("replay produced no paper order")
        if fill is None:
            raise ReplayError("replay produced no paper fill")
        if position is None:
            raise ReplayError("replay produced no paper position")
        result = PaperLoopResult(
            run_id=self._config.run_id,
            trigger_id=str(first.trigger.get("trigger_id") or ""),
            decision_id=first.decision_id,
            decision_validation=first.decision_validation,
            data_quality=first.data_quality,
            order=PaperOrderSummary(
                order_id=str(order.get("order_id") or ""),
                status=str(order.get("status") or ""),
                client_order_id=str(order.get("client_order_id") or ""),
            ),
            fill=PaperFillSummary(
                fill_id=str(fill.get("fill_id") or ""),
                price=str(fill.get("price") or ""),
                fee=str(fill.get("fee") or ""),
                source=str(fill.get("source") or ""),
            ),
            position=PaperPositionSummary(
                position_id=str(position.get("position_id") or ""),
                size=str(position.get("size") or ""),
                notional_usd=str(position.get("notional_usd") or ""),
            ),
            summary_path=str(summary_path),
        )

        summary = {
            "run_id": self._config.run_id,
            "environment": self._config.environment.upper(),
            "symbol": self._config.symbol,
            "period": self._config.period,
            "attempts": 2,
            "deterministic": True,
            "identity_digest": self._identity_digest(),
            "outcome": left,
        }
        write_atomic(summary_path, canonical_json(summary))
        return result


__all__ = [
    "DEFAULT_REPLAY_ENVIRONMENT",
    "DEFAULT_REPLAY_NOW",
    "PAPER_LOOP_NAMESPACE",
    "PaperFillSummary",
    "PaperLoopConfig",
    "PaperLoopOutcome",
    "PaperLoopReplay",
    "PaperLoopResult",
    "PaperOrderSummary",
    "PaperPositionSummary",
    "ReplayRecordingError",
    "ReplayRecordingInvalidError",
    "ReplayRecordingMissingError",
    "validate_run_id",
]
