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

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from dataclasses import replace as replace_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from oft.domain.trigger import CONTRACT_DATA_QUALITIES
from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
    ReplayNonDeterministicError,
    write_atomic,
)
from oft.replay.support import (
    PINNED_OUTCOME_SETTINGS,
    pinned_fixture_paths,
    reject_symlink_components,
)

if TYPE_CHECKING:
    from oft.application.scan_service import ScanRun
    from oft.bootstrap import AppContainer
    from oft.config import Settings
    from oft.ports.llm import LLMPort
    from oft.ports.store import DecisionRecord

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


def _fixture_content_digest(path: Path) -> str:
    """Hash fixture *contents*, so identical fixtures share a digest across checkouts.

    Absolute resolved paths differ between two clones even when every fixture
    byte is identical; hashing the file names (relative to the fixture root) and
    their bytes keeps ``settings_digest`` checkout-independent. A missing path
    hashes the stable ``missing`` marker instead of raising while writing a
    summary (the missing fixture would already have failed the replay earlier).
    """
    resolved = Path(path).expanduser()
    try:
        if resolved.is_dir():
            digest = hashlib.sha256()
            for file_path in sorted(item for item in resolved.rglob("*") if item.is_file()):
                relative = file_path.relative_to(resolved).as_posix()
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update(file_path.read_bytes())
                digest.update(b"\0")
            return digest.hexdigest()
        if resolved.is_file():
            return hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReplayError(f"cannot read replay fixture {resolved}: {exc}") from exc
    return "missing"


class ReplayRecordingError(ReplayError):
    """A recorded LLM output is missing or unusable; replay fails closed."""

    code = "VALIDATION_FAILED"


class ReplayRecordingMissingError(ReplayRecordingError):
    """No recording exists for a request; replay never invents one."""

    code = "DATA_UNAVAILABLE"


class ReplayRecordingInvalidError(ReplayRecordingError):
    """A recording exists but is unreadable, tampered, or for another request."""

    code = "VALIDATION_FAILED"


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
                "recordings_dir is required; pass --recordings-dir first, then populate it "
                "with --capture (offline stub) or --live-llm (explicit provider)."
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
    copy: dict[str, Any] = json.loads(json.dumps(payload))
    source = copy.get("source")
    if isinstance(source, dict):
        copy["source"] = {
            key: value for key, value in source.items() if key not in {"scan_id", "report_id"}
        }
    return copy


def _stable_outcome(outcome: PaperLoopOutcome) -> dict[str, Any]:
    """The projection two replay attempts must agree on exactly.

    Built from ``asdict`` so a new field is compared automatically; only the
    opportunity needs a stable projection because a scan generates two random
    ids.
    """
    stable = asdict(outcome)
    stable["opportunity"] = _stable_opportunity(outcome.opportunity)
    return stable


def _canonical_json(payload: Any) -> str:
    """Deterministic comparison text; the paper loop carries floats, unlike metrics."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
        reject_symlink_components(self._run_root, stop=self._replay_root)
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
        # Pin the fixtures that feed sizing, risk, and data quality explicitly:
        # an ambient OFT_FIXTURES_DIR/OFT_KLINES_FIXTURE/OFT_EXCHANGE_INFO_FIXTURE
        # would otherwise change the outcome on another machine.
        values.update(pinned_fixture_paths())
        # Explicit values win over ambient environment variables, so a replay on
        # another machine cannot silently change fills, fees, balance, or caps.
        values.update(PINNED_OUTCOME_SETTINGS)
        if self._config.live_llm:
            # Honour the configured provider (explicit --provider wins), never
            # default to a live provider when none is named.
            if self._config.provider is not None:
                values["llm_provider"] = self._config.provider
        else:
            values["llm_provider"] = "stub"
        return Settings(**values)

    def _settings_digest(self) -> str:
        """A stable, checkout-independent digest of the inputs behind a replay.

        Fixtures are hashed by content, not by absolute path: two checkouts with
        byte-identical fixtures produce the same digest.
        """
        from oft.config import DEFAULT_LLM_FIXTURES_DIR

        payload = {
            "symbol": self._config.symbol,
            "period": self._config.period,
            "environment": self._config.environment.upper(),
            "archive_fixture_digest": _fixture_content_digest(self._config.archive_fixture_dir),
            "llm_fixtures_digest": _fixture_content_digest(
                self._config.llm_fixtures_dir or DEFAULT_LLM_FIXTURES_DIR
            ),
            "pinned_fixtures": {
                name: _fixture_content_digest(path) for name, path in pinned_fixture_paths().items()
            },
            "llm_model": "fixture",
            "provider": self._config.provider if self._config.live_llm else "stub",
            "pinned": PINNED_OUTCOME_SETTINGS,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

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
        )

    @staticmethod
    def _provider_name(source: LLMPort) -> str:
        return str(getattr(source, "provider", "") or "stub")

    def _attempt_dir(self, attempt: str) -> Path:
        """Resolve one attempt directory and prove it stays in the namespace."""
        raw = self.run_root / attempt
        reject_symlink_components(raw, stop=self.run_root)
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

    @staticmethod
    def _raise_no_opportunity(scan: ScanRun) -> NoReturn:
        llm_state = scan.llm_state or {}
        reason = str(llm_state.get("reason") or "")
        code = str(llm_state.get("error_code") or "")
        if code == "LLM_RECORDING_INVALID":
            raise ReplayRecordingInvalidError(
                "recorded LLM output is invalid or tampered; refusing to replay it. "
                "Inspect and re-capture deliberately (never overwrite a published "
                "recording blindly): " + (reason or "unknown reason")
            )
        if (
            code == "LLM_RECORDING_MISSING"
            or "LLM_RECORDING_MISSING" in reason
            or "no recorded LLM output" in reason
        ):
            raise ReplayRecordingMissingError(
                "no recorded LLM output for role=ANALYSIS; record one with --capture "
                "(offline stub) or --live-llm (explicit provider): " + (reason or "unknown reason")
            )
        raise ReplayError("replay produced no OPEN opportunity; " + (reason or "unknown reason"))

    def run(self) -> PaperLoopResult:
        """Run the loop twice, compare every stable value, and write a summary.

        An existing run root fails closed before any write: replay never
        deletes, resets, or overwrites a previous run. Pass a new ``--run-id``.
        """
        reject_symlink_components(self._run_root, stop=self._replay_root)
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
            "settings_digest": self._settings_digest(),
            "outcome": left,
        }
        write_atomic(summary_path, _canonical_json(summary))
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
