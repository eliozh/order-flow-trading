"""Replay: journal statistics are computed by the same domain code every time."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from oft.domain.journal import DEFINITIONS, compute_stats
from oft.replay.position_management import (
    PositionManagementConfig,
    PositionManagementReplay,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)


def _scenario(tmp_path: Path, recordings: Path, run_id: str) -> dict[str, Any]:
    config = PositionManagementConfig(
        symbol="BTCUSDT",
        archive_fixture_dir=ARCHIVE,
        backfill_start=START,
        backfill_end=END,
        recordings_dir=recordings,
        run_id=run_id,
    )
    result = PositionManagementReplay(data_dir=tmp_path / run_id, config=config).run()
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    return {"result": result, "outcome": summary["outcome"]}


def test_journal_statistics_are_identical_across_two_fresh_stores(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    first = _scenario(tmp_path, position_management_recordings, "journal-a")
    second = _scenario(tmp_path, position_management_recordings, "journal-b")

    assert first["outcome"]["statistics"] == second["outcome"]["statistics"]
    assert first["result"].statistics_status == second["result"].statistics_status


def test_statistics_are_recomputed_from_the_trade_log_by_domain_code(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "journal-domain")
    trade = run["outcome"]["trade_log"]
    statistics = run["outcome"]["statistics"]

    direct = compute_stats([trade], open_positions=0).to_contract()

    assert statistics == direct
    assert statistics["definitions"] == DEFINITIONS
    # This scenario deliberately exercises a partial close, and the documented
    # eligibility rule excludes PARTIAL trades from the metrics while counting
    # them. The result is not a silent zero.
    assert statistics["exclusions"]["partial_trades"] == 1
    if statistics["status"] == "UNDEFINED":
        assert statistics["win_rate"] is None
        assert statistics["expectancy_usd"] is None
        assert statistics["totals"]["net_pnl_usd"] is None
    else:
        assert statistics["sample_size"] == 1
        assert statistics["wins"] in {0, 1}


def test_the_trade_log_is_excluded_as_a_partial_trade_not_as_unreadable(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "journal-exclusions")
    trade = run["outcome"]["trade_log"]
    statistics = run["outcome"]["statistics"]

    assert "PARTIAL" in trade["outcome_tags"]
    assert trade["data_quality"]["status"] == "COMPLETE"
    assert statistics["exclusions"]["unreadable_trades"] == 0
    assert statistics["exclusions"]["outside_period"] == 0
    assert statistics["exclusions"]["partial_trades"] == 1
