"""Shared replay fixtures: one recorded paper-loop capture per test session."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from oft.replay.paper_loop import PaperLoopConfig, PaperLoopReplay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


@pytest.fixture(scope="session")
def replay_recordings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Capture the fixture provider's responses once for every replay test.

    The capture run itself uses the isolated replay namespace under a temporary
    data directory, so it never opens the repository's live store.
    """
    root = tmp_path_factory.mktemp("recorded-llm")
    recordings = root / "recordings"
    config = PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        capture_recordings=True,
        run_id="capture",
    )
    PaperLoopReplay(data_dir=root / "capture-data", config=config).run()
    yield recordings
