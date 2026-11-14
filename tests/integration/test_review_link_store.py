"""Store-level: the review link is atomic, FK-checked, and never touch metrics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_review_flow import _closed_trade

from oft.ports.store import DecisionRecord

_METRICS = (
    "gross_pnl_usd",
    "fees_usd",
    "funding_usd",
    "net_pnl_usd",
    "return_pct",
    "r_multiple",
    "mae_usd",
    "mfe_usd",
    "outcome_tags",
    "data_quality",
    "provenance",
)


def _decision(decision_id: str) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        role="REVIEW",
        decision_type="REVIEW",
        validation_status="VALID",
        raw_output_hash="hash",
        persisted_at="2026-10-08T00:00:00.000Z",
        payload_json="{}",
    )


def _row(settings: Any, trade_id: str) -> tuple[str, str | None]:
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        row = connection.execute(
            "SELECT payload_json, review_decision_id FROM trade_logs WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0]), row[1]


def test_a_missing_trade_is_not_linked(tmp_path: Path) -> None:
    _settings, container, _position, _trade = _closed_trade(tmp_path)

    assert container.store.link_trade_log_review("TRD-MISSING", decision_id="RVW-X") is False


@pytest.mark.parametrize("payload_json", ("not-json", "[1, 2, 3]"))
def test_an_unreadable_or_non_object_payload_raises_and_rolls_back(
    tmp_path: Path, payload_json: str
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    container.store.save_decision(_decision("RVW-LINK"))
    connection = sqlite3.connect(settings.data_dir / "oft.db")
    try:
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (payload_json, trade_id),
        )
        connection.commit()
    finally:
        connection.close()
    before = _row(settings, trade_id)

    with pytest.raises(ValueError):
        container.store.link_trade_log_review(trade_id, decision_id="RVW-LINK")

    assert _row(settings, trade_id) == before
    assert before[1] is None


def test_a_dangling_link_is_rejected_by_the_database_and_rolled_back(
    tmp_path: Path,
) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    before = _row(settings, trade_id)

    with pytest.raises(sqlite3.IntegrityError):
        container.store.link_trade_log_review(trade_id, decision_id="RVW-DOES-NOT-EXIST")

    assert _row(settings, trade_id) == before
    assert json.loads(before[0])["review_decision_id"] is None


def test_overlapping_links_end_with_column_and_payload_agreeing(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    trade_id = trade["trade_id"]
    container.store.save_decision(_decision("RVW-LINK-A"))
    container.store.save_decision(_decision("RVW-LINK-B"))
    original = json.loads(_row(settings, trade_id)[0])

    assert container.store.link_trade_log_review(trade_id, decision_id="RVW-LINK-A")
    assert container.store.link_trade_log_review(trade_id, decision_id="RVW-LINK-B")

    payload_json, column = _row(settings, trade_id)
    payload = json.loads(payload_json)
    assert column == "RVW-LINK-B"
    assert payload["review_decision_id"] == "RVW-LINK-B"
    assert {field: payload[field] for field in _METRICS} == {
        field: original[field] for field in _METRICS
    }
