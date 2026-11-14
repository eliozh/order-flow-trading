"""Replay: PnL terms are reproducible from the recorded position timeline."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.domain.pnl import net_pnl_usd
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
    return {"result": result, "summary": summary, "outcome": summary["outcome"]}


def _stable_summary_bytes(summary_path: str, run_id: str) -> bytes:
    """The summary with the run id normalized, so two runs compare byte for byte."""
    text = Path(summary_path).read_text(encoding="utf-8")
    return text.replace(f'"run_id":"{run_id}"', '"run_id":"RUN"').encode("utf-8")


def test_pnl_terms_are_byte_identical_across_two_fresh_stores(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    first = _scenario(tmp_path, position_management_recordings, "pnl-a")
    second = _scenario(tmp_path, position_management_recordings, "pnl-b")

    left = first["outcome"]["trade_log"]
    right = second["outcome"]["trade_log"]
    assert left["gross_pnl_usd"] == right["gross_pnl_usd"]
    assert left["fees_usd"] == right["fees_usd"]
    assert left["funding_usd"] == right["funding_usd"]
    assert left["net_pnl_usd"] == right["net_pnl_usd"]
    assert left["r_multiple"] == right["r_multiple"]
    assert left["return_pct"] == right["return_pct"]
    assert _stable_summary_bytes(first["result"].summary_path, "pnl-a") == _stable_summary_bytes(
        second["result"].summary_path, "pnl-b"
    )


def test_the_journal_pnl_terms_match_the_domain_arithmetic(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "pnl-domain")
    trade = run["outcome"]["trade_log"]
    position = run["outcome"]["closed_position"]

    # net = gross - fees + funding is the one identity the journal renders; the
    # accumulated gross already includes the earlier partial-close slice.
    assert Decimal(trade["net_pnl_usd"]) == net_pnl_usd(
        Decimal(trade["gross_pnl_usd"]),
        Decimal(trade["fees_usd"]),
        Decimal(trade["funding_usd"]),
    )
    assert Decimal(trade["net_pnl_usd"]) == Decimal(position["realized_pnl_usd"])
    assert Decimal(trade["gross_pnl_usd"]) == (
        Decimal(trade["net_pnl_usd"]) + Decimal(trade["fees_usd"]) - Decimal(trade["funding_usd"])
    )


def test_the_scenario_accrues_recorded_funding_exactly_once_per_settlement(
    tmp_path: Path, position_management_recordings: Path
) -> None:
    run = _scenario(tmp_path, position_management_recordings, "pnl-funding")
    funding = run["outcome"]["funding"]
    trade = run["outcome"]["trade_log"]

    assert funding["accrued"] == 2
    assert funding["reasons"] == []
    # The closed trade consumes the recorded funding term; it is never zeroed.
    assert Decimal(trade["funding_usd"]) < 0
    assert Decimal(trade["net_pnl_usd"]) == net_pnl_usd(
        Decimal(trade["gross_pnl_usd"]),
        Decimal(trade["fees_usd"]),
        Decimal(trade["funding_usd"]),
    )
