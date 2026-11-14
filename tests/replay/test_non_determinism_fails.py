"""Replay fails loudly on non-determinism and on a missing recorded output."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from oft.application.entry_service import EntryRejected
from oft.replay.harness import (
    ReplayError,
    ReplayNonDeterministicError,
)
from oft.replay.paper_loop import (
    PaperLoopConfig,
    PaperLoopReplay,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def _config(recordings: Path, *, run_id: str = "paper-loop") -> PaperLoopConfig:
    return PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id=run_id,
    )


@pytest.mark.parametrize(
    "field",
    ["fill", "trigger", "order", "position", "account", "risk_verdict"],
)
def test_a_non_deterministic_paper_loop_fails_loudly(
    tmp_path: Path,
    replay_recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    calls = {"count": 0}
    original = PaperLoopReplay._run_once

    def flaky(self: PaperLoopReplay, attempt: str):  # type: ignore[no-untyped-def]
        outcome = original(self, attempt)
        calls["count"] += 1
        if calls["count"] == 2:
            value = getattr(outcome, field)
            assert isinstance(value, dict)
            return replace(outcome, **{field: {**value, "mutated": True}})
        return outcome

    monkeypatch.setattr(PaperLoopReplay, "_run_once", flaky)

    with pytest.raises(ReplayNonDeterministicError, match="non-deterministic"):
        PaperLoopReplay(
            data_dir=tmp_path / "data", config=_config(replay_recordings, run_id="flaky")
        ).run()

    summary = tmp_path / "data" / "replay" / "paper-loop" / "flaky" / "summary.json"
    assert not summary.exists()


def test_no_recorded_analysis_output_fails_the_loop(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="no recorded LLM output"):
        PaperLoopReplay(
            data_dir=tmp_path / "data",
            config=_config(tmp_path / "empty-recordings"),
        ).run()


def test_a_missing_entry_recheck_recording_fails_loudly(
    tmp_path: Path, replay_recordings: Path
) -> None:
    recordings = tmp_path / "recordings"
    shutil.copytree(replay_recordings, recordings)
    role_dir = recordings / "ENTRY_RECHECK"
    assert role_dir.is_dir()
    for path in role_dir.glob("*.json"):
        path.unlink()

    with pytest.raises(EntryRejected) as raised:
        PaperLoopReplay(
            data_dir=tmp_path / "data",
            config=_config(recordings, run_id="missing-entry"),
        ).run()

    assert raised.value.code == "LLM_RECORDING_MISSING"
    assert "recorded LLM output" in raised.value.message


@pytest.mark.parametrize("run_id", [".", "..", "---", "a/b", "../escape"])
def test_dot_and_path_like_run_ids_are_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ReplayError, match="run_id"):
        _config(tmp_path, run_id=run_id)


# -- position-management scenario (slice 31) --------------------------------


def _management_config(recordings: Path, *, run_id: str = "management") -> Any:
    from oft.replay.position_management import PositionManagementConfig

    return PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id=run_id,
    )


@pytest.mark.parametrize(
    "field",
    ["management", "funding", "closed_position", "trade_log", "statistics", "review"],
)
def test_a_non_deterministic_management_scenario_fails_loudly(
    tmp_path: Path,
    position_management_recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from oft.replay.position_management import (
        PositionManagementOutcome,
        PositionManagementReplay,
    )

    calls = {"count": 0}
    original = PositionManagementReplay._run_once

    def flaky(self: PositionManagementReplay, attempt: str) -> PositionManagementOutcome:
        outcome = original(self, attempt)
        calls["count"] += 1
        if calls["count"] == 2:
            value = getattr(outcome, field)
            if isinstance(value, tuple):
                return replace(outcome, **{field: (*value, {"mutated": True})})
            assert isinstance(value, dict)
            return replace(outcome, **{field: {**value, "mutated": True}})
        return outcome

    monkeypatch.setattr(PositionManagementReplay, "_run_once", flaky)

    with pytest.raises(ReplayNonDeterministicError, match="non-deterministic"):
        PositionManagementReplay(
            data_dir=tmp_path / "data",
            config=_management_config(position_management_recordings, run_id="flaky"),
        ).run()

    summary = tmp_path / "data" / "replay" / "position-management" / "flaky" / "summary.json"
    assert not summary.exists()


@pytest.mark.parametrize("run_id", [".", "..", "---", "a/b", "../escape"])
def test_dot_and_path_like_management_run_ids_are_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ReplayError, match="run_id"):
        _management_config(tmp_path, run_id=run_id)
