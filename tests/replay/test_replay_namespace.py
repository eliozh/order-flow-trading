"""Replay writes only its namespace; live account/OMS/budget stay untouched."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import position_management_config, run_position_management
from oft.adapters.sqlite_store import SqliteStore
from oft.application.paper_account import PaperAccountService
from oft.clock import FixedClock
from oft.config import Settings
from oft.replay.harness import (
    REPLAY_NAMESPACE,
    ReplayError,
    ReplayLivePathError,
)
from oft.replay.paper_loop import PaperLoopConfig, PaperLoopReplay

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _config(recordings: Path) -> PaperLoopConfig:
    return PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
    )


def test_replay_never_touches_the_live_account_oms_or_budget(
    tmp_path: Path, replay_recordings: Path
) -> None:
    data_dir = tmp_path / "live"
    settings = Settings(env="test", port=8765, data_dir=data_dir)
    live_store = SqliteStore(settings)
    live_store.initialize()
    PaperAccountService(settings=settings, store=live_store, clock=FixedClock(NOW)).ensure()
    live_db = data_dir / "oft.db"
    live_db_bytes = live_db.read_bytes()
    live_part = data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT" / "part-0.parquet"
    live_part.parent.mkdir(parents=True)
    live_part.write_bytes(b"live-part")
    before = {path: path.read_bytes() for path in data_dir.rglob("*") if path.is_file()}

    result = PaperLoopReplay(data_dir=data_dir, config=_config(replay_recordings)).run()

    assert live_db.read_bytes() == live_db_bytes
    assert live_part.read_bytes() == b"live-part"
    assert live_store.list_orders() == ()
    assert live_store.list_fills() == ()
    assert live_store.list_positions() == ()
    assert live_store.list_budget_records() == ()

    replay_root = (data_dir / REPLAY_NAMESPACE).resolve()
    assert Path(result.summary_path).resolve().is_relative_to(replay_root)
    new_files = [path for path in data_dir.rglob("*") if path.is_file() and path not in before]
    assert new_files
    for path in new_files:
        assert path.resolve().is_relative_to(replay_root), path
    for path, content in before.items():
        assert path.read_bytes() == content


def test_a_live_environment_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReplayLivePathError, match="live"):
        PaperLoopConfig(
            symbol="BTCUSDT",
            archive_fixture_dir=ARCHIVE,
            backfill_start=START,
            backfill_end=END,
            recordings_dir=tmp_path,
            environment="live",
        )


def test_the_cli_exits_non_zero_on_an_attempted_live_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        [
            "replay",
            "run",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(tmp_path / "recordings"),
            "--environment",
            "live",
        ],
    )

    assert result.exit_code != 0
    assert "live" in result.output.lower()


def test_a_symlinked_run_root_is_rejected_before_any_write(
    tmp_path: Path, replay_recordings: Path
) -> None:
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_root = data_dir / REPLAY_NAMESPACE / "paper-loop" / "symlinked"
    run_root.parent.mkdir(parents=True)
    run_root.symlink_to(outside, target_is_directory=True)
    config = PaperLoopConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=replay_recordings,
        run_id="symlinked",
    )

    with pytest.raises(ReplayError, match="symlink"):
        PaperLoopReplay(data_dir=data_dir, config=config).run()

    assert list(outside.rglob("*")) == []


# -- position-management scenario (slice 31) --------------------------------


def _management_config(recordings: Path, *, run_id: str = "management") -> Any:
    return position_management_config(recordings, run_id=run_id)


def test_the_management_scenario_never_touches_the_live_account_or_ledgers(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    from oft.replay.position_management import PositionManagementReplay

    data_dir = tmp_path / "live"
    settings = Settings(env="test", port=8765, data_dir=data_dir)
    live_store = SqliteStore(settings)
    live_store.initialize()
    PaperAccountService(settings=settings, store=live_store, clock=FixedClock(NOW)).ensure()
    live_db = data_dir / "oft.db"
    live_db_bytes = live_db.read_bytes()
    live_part = data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT" / "part-0.parquet"
    live_part.parent.mkdir(parents=True)
    live_part.write_bytes(b"live-part")
    before = {path: path.read_bytes() for path in data_dir.rglob("*") if path.is_file()}

    result = PositionManagementReplay(
        data_dir=data_dir,
        config=_management_config(position_management_recordings),
    ).run()

    assert live_db.read_bytes() == live_db_bytes
    assert live_part.read_bytes() == b"live-part"
    assert live_store.list_orders() == ()
    assert live_store.list_fills() == ()
    assert live_store.list_positions() == ()
    assert live_store.list_trade_logs(limit=None) == ()
    assert live_store.list_budget_records() == ()

    replay_root = (data_dir / REPLAY_NAMESPACE).resolve()
    assert Path(result.summary_path).resolve().is_relative_to(replay_root)
    new_files = [path for path in data_dir.rglob("*") if path.is_file() and path not in before]
    assert new_files
    for path in new_files:
        assert path.resolve().is_relative_to(replay_root), path
    for path, content in before.items():
        assert path.read_bytes() == content


def test_the_management_scenario_refuses_a_live_environment(tmp_path: Path) -> None:
    from oft.replay.position_management import PositionManagementConfig

    with pytest.raises(ReplayLivePathError, match="live"):
        PositionManagementConfig(
            symbol="BTCUSDT",
            archive_fixture_dir=ARCHIVE,
            backfill_start=START,
            backfill_end=END,
            recordings_dir=tmp_path,
            environment="live",
        )


def test_the_cli_refuses_a_live_management_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        [
            "replay",
            "run",
            "--scenario",
            "position-management",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(tmp_path / "recordings"),
            "--environment",
            "live",
        ],
    )

    assert result.exit_code != 0
    assert "live" in result.output.lower()


def test_a_symlinked_management_run_root_is_rejected_before_any_write(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    from oft.replay.position_management import PositionManagementReplay

    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_root = data_dir / REPLAY_NAMESPACE / "position-management" / "symlinked"
    run_root.parent.mkdir(parents=True)
    run_root.symlink_to(outside, target_is_directory=True)
    config = position_management_config(position_management_recordings, run_id="symlinked")

    with pytest.raises(ReplayError, match="symlink"):
        PositionManagementReplay(data_dir=data_dir, config=config).run()

    assert list(outside.rglob("*")) == []


def test_rerunning_an_existing_management_run_fails_before_writing_anything(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run_position_management(tmp_path, position_management_recordings, "existing")
    root = tmp_path / "data"
    before_files = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    before_dirs = {path.relative_to(root) for path in root.rglob("*") if path.is_dir()}

    with pytest.raises(ReplayError, match="already exists.*--run-id"):
        run_position_management(tmp_path, position_management_recordings, "existing")

    after_files = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    after_dirs = {path.relative_to(root) for path in root.rglob("*") if path.is_dir()}
    assert after_files == before_files
    assert after_dirs == before_dirs


def test_capture_writes_recordings_outside_the_replay_namespace(
    tmp_path: Path,
) -> None:
    from oft.replay.position_management import PositionManagementReplay

    recordings = tmp_path / "recordings-outside"
    data_dir = tmp_path / "data"
    config = replace(
        position_management_config(recordings, run_id="capture-layout"),
        capture_recordings=True,
    )

    result = PositionManagementReplay(data_dir=data_dir, config=config).run()

    expected_summary = (
        data_dir / REPLAY_NAMESPACE / "position-management" / "capture-layout" / "summary.json"
    ).resolve()
    assert Path(result.summary_path).resolve() == expected_summary
    replay_root = (data_dir / REPLAY_NAMESPACE).resolve()
    recorded = sorted(recordings.rglob("*.json"))
    assert {path.parent.name for path in recorded} == {
        "ANALYSIS",
        "ENTRY_RECHECK",
        "MANAGEMENT",
        "REVIEW",
    }
    for path in recorded:
        assert re.fullmatch(r"[0-9a-f]{64}", path.stem), path
        assert not path.resolve().is_relative_to(replay_root), path
