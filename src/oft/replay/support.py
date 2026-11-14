"""Shared safety and fixture-pinning helpers for deterministic replays.

The paper-loop and position-management scenarios run the same domain code in
isolated replay stores. They must agree on which ambient settings are pinned,
on the path-safety rules, and on how a run-generated id is projected out, so
those helpers live here once instead of being imported from one scenario by the
other.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

from oft.config import Environment
from oft.replay.harness import ReplayError
from oft.replay.llm_source import RecordingErrorCode

if TYPE_CHECKING:
    from oft.application.scan_service import ScanRun
    from oft.config import Settings

# A replay may only run in an environment that cannot reach a live order path.
READ_ONLY_ENVIRONMENTS: frozenset[Environment] = frozenset({Environment.PAPER, Environment.TEST})

# The only two ids a scan generates randomly; every other value is pinned.
UNSTABLE_OPPORTUNITY_SOURCE_IDS = frozenset({"scan_id", "report_id"})

# Every setting that can change a fill, fee, account balance, or risk verdict is
# pinned here. ``Settings`` is a ``BaseSettings`` model, so an ambient
# ``OFT_PAPER_*``/``OFT_RISK_*`` value would otherwise leak into a replay on a
# different machine and surface only as a generic non-determinism error. The
# mapping is frozen so a scenario cannot silently mutate the pinned baseline.
PINNED_OUTCOME_SETTINGS: Mapping[str, Any] = MappingProxyType(
    {
        "paper_initial_balance": "10000.00",
        "paper_slippage_bps": "2",
        "paper_taker_fee_bps": "5",
        "risk_per_trade_pct": 0.8,
        "risk_max_concurrent_positions": 3,
        "risk_daily_loss_pct": 3.0,
        "risk_max_leverage": 5.0,
        "risk_max_notional_pct": 20.0,
        "risk_max_slippage_bps": 25.0,
        "risk_max_spread_bps": 15.0,
        "risk_stale_mark_seconds": 60,
    }
)


def validate_pinned_settings() -> None:
    """Fail at import time if a pinned key is not a real ``Settings`` field.

    A typo would otherwise be silently ignored by Pydantic (``extra="ignore"``)
    and the intended pin would never reach the replay container.
    """
    from oft.config import Settings

    unknown = sorted(set(PINNED_OUTCOME_SETTINGS) - set(Settings.model_fields))
    if unknown:
        raise RuntimeError(f"pinned replay settings are not Settings fields: {unknown}")


validate_pinned_settings()


class ReplayRecordingError(ReplayError):
    """A recorded LLM output is missing or unusable; replay fails closed."""

    code = RecordingErrorCode.INVALID


class ReplayRecordingMissingError(ReplayRecordingError):
    """No recording exists for a request; replay never invents one."""

    code = RecordingErrorCode.MISSING


class ReplayRecordingInvalidError(ReplayRecordingError):
    """A recording exists but is unreadable, tampered, or for another request."""

    code = RecordingErrorCode.INVALID


def raise_no_replay_opportunity(scan: ScanRun) -> NoReturn:
    """Translate a scan that produced no OPEN opportunity into a typed failure.

    The classification decides only from the typed ``error_code`` the scan
    recorded; the English ``reason`` is for humans and may be reworded without
    changing replay behaviour. A missing/INVALID recording is reported as the
    same typed replay error on every scenario, so an ``ANALYSIS`` recording
    failure is never downgraded to an untyped "no opportunity" error.
    """
    llm_state = scan.llm_state or {}
    reason = str(llm_state.get("reason") or "")
    code = str(llm_state.get("error_code") or "")
    if code == RecordingErrorCode.INVALID:
        raise ReplayRecordingInvalidError(
            "recorded LLM output is invalid or tampered; refusing to replay it. "
            "Inspect and re-capture deliberately (never overwrite a published "
            "recording blindly): " + (reason or "unknown reason")
        )
    if code == RecordingErrorCode.MISSING:
        raise ReplayRecordingMissingError(
            "no recorded LLM output for role=ANALYSIS; record one with --capture "
            "(offline stub) or --live-llm (explicit provider): " + (reason or "unknown reason")
        )
    raise ReplayError("replay produced no OPEN opportunity; " + (reason or "unknown reason"))


def pinned_fixture_paths() -> dict[str, Path]:
    """The committed fixtures that seed sizing/risk and data-quality outcomes.

    The exchange-info fixture seeds the symbol registry (tick/step/min-notional
    drive entry sizing and the risk gate); the klines fixture changes the
    cross-check ``quality_reasons`` and therefore ``data_quality``. Pinning them
    explicitly keeps an ambient ``OFT_*_FIXTURE`` value from changing a replay.
    """
    from oft.config import (
        DEFAULT_EXCHANGE_INFO_FIXTURE,
        DEFAULT_FIXTURES_DIR,
        DEFAULT_KLINES_FIXTURE,
    )

    return {
        "fixtures_dir": DEFAULT_FIXTURES_DIR,
        "klines_fixture": DEFAULT_KLINES_FIXTURE,
        "exchange_info_fixture": DEFAULT_EXCHANGE_INFO_FIXTURE,
    }


def fixture_content_digest(path: Path) -> str:
    """Hash fixture *contents*, so identical fixtures share a digest across checkouts.

    Absolute resolved paths differ between two clones even when every fixture
    byte is identical; hashing the file names (relative to the fixture root) and
    their bytes keeps a settings/identity digest checkout-independent. A missing
    path hashes the stable ``missing`` marker instead of raising while writing a
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


def canonical_json(payload: Any) -> str:
    """Deterministic comparison text shared by every replay scenario."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def project_stable_ids(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop exactly the two run-generated ids from a source-like mapping."""
    return {
        key: value for key, value in payload.items() if key not in UNSTABLE_OPPORTUNITY_SOURCE_IDS
    }


def project_stable_opportunity_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly drop the two ids a scan generates randomly from ``source``.

    The projection is by name, not a blanket recursion: only ``source`` carries
    run-generated ids, and every other value must compare exactly.
    """
    copy: dict[str, Any] = json.loads(json.dumps(payload))
    source = copy.get("source")
    if isinstance(source, dict):
        copy["source"] = project_stable_ids(source)
    return copy


def build_replay_settings(
    *,
    attempt_dir: Path,
    archive_fixture_dir: Path,
    llm_fixtures_dir: Path | None,
    live_llm: bool,
    provider: str | None,
) -> Settings:
    """Build the isolated settings one replay attempt runs against.

    Explicit pinned values win over ambient ``OFT_*`` environment variables, so
    a replay on another machine cannot silently change fills, fees, balance, or
    caps. In ``--live-llm`` mode the configured provider is honoured (an
    explicit ``--provider`` wins) but never defaulted; otherwise the offline
    stub is pinned.
    """
    from oft.config import DEFAULT_LLM_FIXTURES_DIR, Settings

    values: dict[str, Any] = {
        "env": "test",
        "port": 8765,
        "data_dir": attempt_dir,
        "archive_fixture_dir": archive_fixture_dir,
        "llm_model": "fixture",
        "llm_fixtures_dir": llm_fixtures_dir or DEFAULT_LLM_FIXTURES_DIR,
    }
    values.update(pinned_fixture_paths())
    values.update(PINNED_OUTCOME_SETTINGS)
    if live_llm:
        # Leaving the field unset lets ``Settings`` resolve the documented
        # OFT_LLM_PROVIDER; never default to a live provider when none is named.
        if provider is not None:
            values["llm_provider"] = provider
    else:
        values["llm_provider"] = "stub"
    return Settings(**values)


def resolve_attempt_dir(*, run_root: Path, namespace_root: Path, attempt: str) -> Path:
    """Resolve one attempt directory and prove it stays in the namespace."""
    raw = run_root / attempt
    reject_symlink_components(raw, stop=run_root)
    resolved = raw.resolve()
    if not resolved.is_relative_to(namespace_root.resolve()):
        raise ReplayError(f"replay {attempt} escaped the replay namespace")
    return resolved


def resolve_recordings_dir(recordings_dir: Path) -> Path:
    """The absolute recordings root; never relative to the process cwd."""
    return Path(recordings_dir).expanduser().resolve()


def reject_symlink_components(path: Path, *, stop: Path) -> None:
    """Refuse a symlinked path component between ``path`` and ``stop`` (lstat)."""
    current = path
    while True:
        if current.is_symlink():
            raise ReplayError(f"replay path component is a symlink: {current}")
        if current == stop or current.parent == current:
            return
        current = current.parent


def guard_replay_run_root(
    *,
    run_root: Path,
    replay_root: Path,
    scenario_root: Path,
    run_id: str,
) -> None:
    """Refuse a symlinked or escaping run root before a scenario resolves it.

    Symlink components are checked up to ``replay_root`` (never further up), and
    the resolved run root must stay inside the scenario namespace.
    """
    reject_symlink_components(run_root, stop=replay_root)
    if not run_root.resolve().is_relative_to(scenario_root.resolve()):
        raise ReplayError(f"replay run {run_id!r} escapes {scenario_root}; pass a plain run id")


def guard_new_run_root(*, run_root: Path, replay_root: Path, run_id: str) -> None:
    """Fail closed before any write when a run root already exists.

    Replay never deletes, resets, or overwrites a previous run; the operator is
    told to pass a new ``--run-id``.
    """
    reject_symlink_components(run_root, stop=replay_root)
    if run_root.exists():
        raise ReplayError(
            f"run '{run_id}' already exists at {run_root}; "
            "pass a new --run-id (replay never deletes or overwrites a run)"
        )


def resolve_summary_path(*, run_root: Path, replay_root: Path) -> Path:
    """Resolve the summary path and refuse one that escaped the replay namespace."""
    summary_path = (run_root / "summary.json").resolve()
    if not summary_path.is_relative_to(replay_root.resolve()):
        raise ReplayError("replay summary escaped the replay namespace")
    return summary_path


def replay_identity_digest(
    *,
    scenario: str,
    scenario_version: str,
    symbol: str,
    period: str,
    environment: str,
    archive_fixture_dir: Path,
    llm_fixtures_dir: Path | None,
    recordings_dir: Path | None = None,
    live_llm: bool = False,
    provider: str | None = None,
    llm_model: str = "fixture",
) -> str:
    """A stable, checkout-independent digest of every input behind a replay.

    Fixtures are hashed by content, not by absolute path: two checkouts with
    byte-identical fixtures produce the same digest. The optional recordings
    digest keeps a read-only replay sensitive to the recorded LLM inputs it
    consumed.
    """
    from oft.config import DEFAULT_LLM_FIXTURES_DIR

    payload: dict[str, Any] = {
        "scenario": scenario,
        "scenario_version": scenario_version,
        "symbol": symbol,
        "period": period,
        "environment": environment,
        "archive_fixture_digest": fixture_content_digest(archive_fixture_dir),
        "llm_fixtures_digest": fixture_content_digest(llm_fixtures_dir or DEFAULT_LLM_FIXTURES_DIR),
        "pinned_fixtures": {
            name: fixture_content_digest(path) for name, path in pinned_fixture_paths().items()
        },
        "recordings_digest": (
            fixture_content_digest(recordings_dir) if recordings_dir is not None else None
        ),
        "provider": provider if live_llm else "stub",
        "llm_model": llm_model,
        "pinned": dict(PINNED_OUTCOME_SETTINGS),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "PINNED_OUTCOME_SETTINGS",
    "READ_ONLY_ENVIRONMENTS",
    "UNSTABLE_OPPORTUNITY_SOURCE_IDS",
    "ReplayRecordingError",
    "ReplayRecordingInvalidError",
    "ReplayRecordingMissingError",
    "build_replay_settings",
    "canonical_json",
    "fixture_content_digest",
    "guard_new_run_root",
    "guard_replay_run_root",
    "pinned_fixture_paths",
    "project_stable_ids",
    "project_stable_opportunity_source",
    "raise_no_replay_opportunity",
    "reject_symlink_components",
    "replay_identity_digest",
    "resolve_attempt_dir",
    "resolve_recordings_dir",
    "resolve_summary_path",
    "validate_pinned_settings",
]
