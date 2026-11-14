"""Signal-vs-outcome comparison helpers and formula-safe CSV construction."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from oft.domain.journal import (
    CSV_COLUMNS,
    csv_document,
    csv_row,
    decision_vs_outcome,
    neutralize_csv_cell,
    trigger_to_fill_bps,
    zone_vs_fill,
)


def _trade(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trade_id": "TRD-1",
        "position_id": "POS-1",
        "environment": "PAPER",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "opened_at": "2026-11-13T12:00:00.000Z",
        "closed_at": "2026-11-14T11:00:00.000Z",
        "entry_avg_price": "70000.00",
        "exit_avg_price": "69000.00",
        "size": "0.01",
        "gross_pnl_usd": "-10.00000000",
        "fees_usd": "0.35000000",
        "funding_usd": "0",
        "net_pnl_usd": "-10.35000000",
        "r_multiple": -1.0,
        "mae_usd": "-12.50000000",
        "mfe_usd": "4.25000000",
        "return_pct": -1.0,
        "outcome_tags": ["LOSS", "STOPPED"],
        "data_quality": {"status": "COMPLETE", "reasons": []},
    }
    payload.update(overrides)
    return payload


# -- formula neutralization ------------------------------------------------


def test_formula_prefixed_cells_are_neutralized() -> None:
    assert neutralize_csv_cell("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert neutralize_csv_cell("+1") == "'+1"
    assert neutralize_csv_cell("-1") == "'-1"
    assert neutralize_csv_cell("@cmd") == "'@cmd"


def test_a_plain_cell_is_not_changed() -> None:
    assert neutralize_csv_cell("BTCUSDT") == "BTCUSDT"
    assert neutralize_csv_cell("") == ""
    assert neutralize_csv_cell(None) == ""


@pytest.mark.parametrize(
    "value",
    ["\t=1+1", "\r=1+1", "\n=1+1", " =1+1", "\t@cmd", "  -1"],
)
def test_leading_control_or_whitespace_before_a_formula_is_neutralized(value: str) -> None:
    assert neutralize_csv_cell(value) == f"'{value}"


def test_leading_control_without_a_formula_is_still_neutralized() -> None:
    assert neutralize_csv_cell("\tplain") == "'\tplain"


@pytest.mark.parametrize("value", ["=1+1", "+1+1", "-1+1", "@1+1"])
def test_every_formula_prefix_is_neutralized(value: str) -> None:
    assert neutralize_csv_cell(value) == f"'{value}"


# -- zone vs fill ----------------------------------------------------------


def test_an_entry_inside_the_zone_is_reported_inside() -> None:
    result = zone_vs_fill("70000", "69000", "71000")

    assert result["status"] == "INSIDE"
    assert result["inside"] is True
    assert result["distance_bps"] is None
    assert result["reasons"] == []


def test_an_entry_below_the_zone_reports_the_low_distance_in_bps() -> None:
    result = zone_vs_fill("68000", "69000", "71000")

    assert result["status"] == "BELOW_ZONE"
    assert result["inside"] is False
    assert result["distance_bps"] == float(Decimal("1000") / Decimal("69000") * Decimal("10000"))


def test_an_entry_above_the_zone_reports_the_high_distance_in_bps() -> None:
    result = zone_vs_fill("72000", "69000", "71000")

    assert result["status"] == "ABOVE_ZONE"
    assert result["distance_bps"] == float(Decimal("1000") / Decimal("71000") * Decimal("10000"))


def test_a_missing_zone_is_na_with_a_reason() -> None:
    result = zone_vs_fill("70000", None, None)

    assert result["status"] == "N/A"
    assert result["inside"] is None
    assert result["reasons"]


def test_a_missing_entry_is_na_with_a_reason() -> None:
    result = zone_vs_fill(None, "69000", "71000")

    assert result["status"] == "N/A"
    assert result["distance_bps"] is None
    assert "entry average price" in result["reasons"][0]


# -- trigger -> fill slippage ---------------------------------------------


def test_long_slippage_is_positive_when_the_fill_is_adverse() -> None:
    # Fill above the trigger reference for a LONG is adverse.
    assert trigger_to_fill_bps("LONG", "70100", "70000") == 14.285714285714286


def test_short_slippage_is_positive_when_the_fill_is_adverse() -> None:
    # Fill below the trigger reference for a SHORT is adverse.
    assert trigger_to_fill_bps("SHORT", "69900", "70000") == 14.285714285714286


def test_slippage_is_none_when_a_price_is_missing_or_invalid() -> None:
    assert trigger_to_fill_bps("LONG", None, "70000") is None
    assert trigger_to_fill_bps("LONG", "70000", None) is None
    assert trigger_to_fill_bps("LONG", "70000", "0") is None
    assert trigger_to_fill_bps("LONG", "oops", "70000") is None
    assert trigger_to_fill_bps("NEUTRAL", "70000", "70000") is None


# -- decision vs outcome ---------------------------------------------------


def test_a_matching_direction_and_a_profit_is_matched() -> None:
    result = decision_vs_outcome("ENTER_LONG", "LONG", "10.00000000")

    assert result["status"] == "OK"
    assert result["direction_agrees"] is True
    assert result["matched"] is True


def test_a_matching_direction_with_a_loss_is_not_matched() -> None:
    result = decision_vs_outcome("ENTER_LONG", "LONG", "-10.00000000")

    assert result["direction_agrees"] is True
    assert result["matched"] is False


def test_a_no_entry_decision_disagrees_with_an_executed_trade() -> None:
    result = decision_vs_outcome("NO_ENTRY", "LONG", "10.00000000")

    assert result["direction_agrees"] is False
    assert result["matched"] is False


def test_a_missing_decision_is_na_and_matched_is_null() -> None:
    result = decision_vs_outcome(None, "LONG", "10.00000000")

    assert result["status"] == "N/A"
    assert result["direction_agrees"] is None
    assert result["matched"] is None
    assert result["reasons"]


def test_an_unreadable_net_makes_the_decision_match_partial() -> None:
    result = decision_vs_outcome("ENTER_LONG", "LONG", "not-money")

    assert result["status"] == "PARTIAL"
    assert result["direction_agrees"] is True
    assert result["matched"] is None
    assert result["reasons"]


# -- CSV -------------------------------------------------------------------


def test_csv_rows_only_carry_trade_log_read_fields() -> None:
    row = csv_row(_trade())

    assert len(row) == len(CSV_COLUMNS)
    rendered = dict(zip(CSV_COLUMNS, row, strict=True))
    assert rendered["trade_id"] == "TRD-1"
    assert rendered["data_quality_status"] == "COMPLETE"
    assert "decision_quality" not in rendered
    assert "provenance" not in rendered
    assert "prompt" not in " ".join(CSV_COLUMNS)


def test_csv_document_neutralizes_formula_cells_and_counts_them() -> None:
    export = csv_document([_trade(symbol="=BTCUSDT", outcome_tags=["@WIN"])])

    assert export.rows == 1
    assert export.text.splitlines()[0] == ",".join(CSV_COLUMNS)
    assert "'=BTCUSDT" in export.text
    assert "'@WIN" in export.text
    # Every rendered negative money/R/return cell also starts with "-" and is
    # neutralized, so the count covers the symbol, the tag, and those cells.
    assert export.neutralized_cells == 7


def test_csv_document_quotes_commas_safely() -> None:
    export = csv_document([_trade(outcome_tags=["WIN", "MARKET_CLOSE"])])

    assert export.rows == 1
    assert '"WIN|MARKET_CLOSE"' in export.text or "WIN|MARKET_CLOSE" in export.text


def test_csv_row_handles_missing_or_invalid_quality_and_tags() -> None:
    without_quality = csv_row({"trade_id": "TRD-1"})
    assert without_quality[0] == "TRD-1"
    assert without_quality[-1] == ""

    invalid_quality = csv_row({"data_quality": {"status": 5}, "outcome_tags": "not-a-list"})
    assert invalid_quality[-1] == ""
