"""Replay fails loudly on non-determinism and on a missing recorded output."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from oft.replay.harness import (
    PaperLoopConfig,
    PaperLoopReplay,
    ReplayError,
    ReplayNonDeterministicError,
)

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


def test_a_non_deterministic_paper_loop_fails_loudly(
    tmp_path: Path, replay_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}
    original = PaperLoopReplay._run_once

    def flaky(self: PaperLoopReplay, attempt: str):  # type: ignore[no-untyped-def]
        outcome = original(self, attempt)
        calls["count"] += 1
        if calls["count"] == 2:
            assert outcome.fill is not None
            return replace(outcome, fill={**outcome.fill, "price": "1.00"})
        return outcome

    monkeypatch.setattr(PaperLoopReplay, "_run_once", flaky)

    with pytest.raises(ReplayNonDeterministicError, match="non-deterministic"):
        PaperLoopReplay(
            data_dir=tmp_path / "data", config=_config(replay_recordings, run_id="flaky")
        ).run()

    summary = tmp_path / "data" / "replay" / "paper-loop" / "flaky" / "summary.json"
    assert not summary.exists()


def test_no_recorded_output_fails_the_loop(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="no recorded LLM output"):
        PaperLoopReplay(
            data_dir=tmp_path / "data",
            config=_config(tmp_path / "empty-recordings"),
        ).run()
