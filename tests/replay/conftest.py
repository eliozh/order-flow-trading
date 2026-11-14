"""Shared replay fixtures and position-management scenario factories."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from oft.replay.paper_loop import PaperLoopConfig, PaperLoopReplay
from oft.replay.position_management import (
    PositionManagementConfig,
    PositionManagementReplay,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def position_management_config(
    recordings: Path, *, run_id: str = "management"
) -> PositionManagementConfig:
    """The capture/replay config every position-management test shares."""
    return PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id=run_id,
    )


def run_position_management(
    tmp_path: Path,
    recordings: Path,
    run_id: str = "management",
    *,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one scenario and return its result, summary, and stable outcome.

    ``data_dir`` stays per-test so pnl can isolate each run id while the other
    suites share one data root.
    """
    result = PositionManagementReplay(
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
        config=position_management_config(recordings, run_id=run_id),
    ).run()
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    return {"result": result, "summary": summary, "outcome": summary["outcome"]}


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


@pytest.fixture(scope="session")
def position_management_recordings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Capture ANALYSIS/ENTRY_RECHECK/MANAGEMENT/REVIEW once for the scenario.

    Capture runs in the isolated replay namespace and its second attempt reads
    the recordings written by the first, so the fixture itself proves the
    capture path is replayable before any test consumes it.
    """
    root = tmp_path_factory.mktemp("recorded-management")
    recordings = root / "recordings"
    config = PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        capture_recordings=True,
        run_id="capture",
    )
    PositionManagementReplay(data_dir=root / "capture-data", config=config).run()
    yield recordings
