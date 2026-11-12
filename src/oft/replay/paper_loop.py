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
from dataclasses import dataclass
from dataclasses import replace as replace_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
    write_atomic,
)

if TYPE_CHECKING:
    from oft.application.scan_service import ScanRun
    from oft.bootstrap import AppContainer
    from oft.config import Settings
    from oft.ports.llm import LLMPort

PAPER_LOOP_NAMESPACE = "paper-loop"
DEFAULT_REPLAY_ENVIRONMENT = "PAPER"
DEFAULT_REPLAY_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
_READ_ONLY_ENVIRONMENTS = frozenset({"PAPER", "TEST"})
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


def _reject_symlink_components(path: Path, *, stop: Path) -> None:
    """Refuse a symlinked path component between ``path`` and ``stop`` (lstat)."""
    current = path
    while True:
        if current.is_symlink():
            raise ReplayError(f"replay path component is a symlink: {current}")
        if current == stop or current.parent == current:
            return
        current = current.parent


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
        validate_run_id(self.run_id)
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


class PaperLoopReplay:
    """Run the paper loop twice under replay and prove it is deterministic."""

    def __init__(self, *, data_dir: Path, config: PaperLoopConfig) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._config = config
        self._replay_root = self._data_dir / REPLAY_NAMESPACE
        self._paper_loop_root = self._replay_root / PAPER_LOOP_NAMESPACE
        self._run_root = self._paper_loop_root / config.run_id
        _reject_symlink_components(self._run_root, stop=self._replay_root)
        if not self._run_root.resolve().is_relative_to(self._paper_loop_root.resolve()):
            raise ReplayError(
                f"replay run {config.run_id!r} escapes {self._paper_loop_root}; pass a plain run id"
            )

    @property
    def replay_root(self) -> Path:
        return self._replay_root

    @property
    def run_root(self) -> Path:
        return self._run_root

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

    def _source(
        self,
        container: AppContainer,
        settings: Settings,
        attempt: str,
    ) -> LLMPort:
        from oft.adapters.llm_stub import LLMStub
        from oft.application.prompt_service import secret_values_from_settings
        from oft.replay.llm_source import RecordedLLMSource, RecordingLLMSource

        if not (self._config.live_llm or self._config.capture_recordings):
            return RecordedLLMSource(recordings_dir=self._recordings_dir())
        if attempt != _ATTEMPT_1:
            # Attempt-2 reads exactly what attempt-1 recorded; the provider is
            # never called twice and cannot simply agree with itself.
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
    def _provider_name(source: LLMPort) -> str:
        return str(getattr(source, "provider", "") or "stub")

    def _attempt_dir(self, attempt: str) -> Path:
        """Resolve one attempt directory and prove it stays in the namespace."""
        raw = self.run_root / attempt
        _reject_symlink_components(raw, stop=self.run_root)
        resolved = raw.resolve()
        if not resolved.is_relative_to(self._paper_loop_root.resolve()):
            raise ReplayError(f"replay {attempt} escaped the replay namespace")
        return resolved

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
    def _raise_no_opportunity(scan: ScanRun) -> NoReturn:
        llm_state = scan.llm_state or {}
        reason = str(llm_state.get("reason") or "")
        if "recorded LLM output" in reason or "LLM_RECORDING_MISSING" in reason:
            raise ReplayError(
                "no recorded LLM output for role=ANALYSIS; record one or pass --live-llm: " + reason
            )
        raise ReplayError("replay produced no OPEN opportunity; " + (reason or "unknown reason"))

    def run(self) -> PaperLoopResult:
        """Run the loop twice, compare every stable value, and write a summary.

        An existing run root fails closed before any write: replay never
        deletes, resets, or overwrites a previous run. Pass a new ``--run-id``.
        """
        _reject_symlink_components(self._run_root, stop=self._replay_root)
        if self._run_root.exists():
            raise ReplayError(
                f"run '{self._config.run_id}' already exists at {self._run_root}; "
                "pass a new --run-id (replay never deletes or overwrites a run)"
            )

        first = self._run_once(_ATTEMPT_1)
        second = self._run_once(_ATTEMPT_2)
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
        write_atomic(summary_path, _canonical_json(summary))

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


__all__ = [
    "DEFAULT_REPLAY_ENVIRONMENT",
    "DEFAULT_REPLAY_NOW",
    "PAPER_LOOP_NAMESPACE",
    "PaperLoopConfig",
    "PaperLoopOutcome",
    "PaperLoopReplay",
    "PaperLoopResult",
    "validate_run_id",
]
