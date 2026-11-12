"""Replay writes only its namespace; live account/OMS/budget stay untouched."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.application.paper_account import PaperAccountService
from oft.clock import FixedClock
from oft.config import Settings
from oft.replay.harness import (
    REPLAY_NAMESPACE,
    PaperLoopConfig,
    PaperLoopReplay,
    ReplayLivePathError,
)

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
