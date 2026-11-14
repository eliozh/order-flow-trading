"""Validation: the journal statistics/comparison schemas enforce their invariants."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from oft.api.schemas.journal import (
    JournalAvgR,
    JournalCompareData,
    JournalExportData,
    JournalFirstTouchData,
    JournalSlippageData,
    JournalStatsData,
    JournalZoneData,
)


def _undefined_stats(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "UNDEFINED",
        "reason": "metrics undefined: no closed trades have been recorded",
        "period": None,
        "sample_size": 0,
        "denominator": 0,
        "exclusions": {
            "open_positions": 0,
            "partial_trades": 0,
            "unreadable_trades": 0,
            "outside_period": 0,
        },
        "definitions": {"net": "net = gross - fees + funding"},
        "wins": 0,
        "losses": 0,
        "ties": 0,
        "win_rate": None,
        "expectancy_usd": None,
        "avg_r": {"value": None, "samples": 0, "excluded_null_r": 0},
        "totals": {
            "gross_pnl_usd": None,
            "fees_usd": None,
            "funding_usd": None,
            "net_pnl_usd": None,
        },
        "data_quality": "MISSING",
    }
    payload.update(overrides)
    return payload


def _defined_stats(**overrides: Any) -> dict[str, Any]:
    payload = {
        "status": "DEFINED",
        "reason": None,
        "period": None,
        "sample_size": 2,
        "denominator": 2,
        "exclusions": {
            "open_positions": 0,
            "partial_trades": 0,
            "unreadable_trades": 0,
            "outside_period": 0,
        },
        "definitions": {"net": "net = gross - fees + funding"},
        "wins": 1,
        "losses": 1,
        "ties": 0,
        "win_rate": 0.5,
        "expectancy_usd": "1",
        "avg_r": {"value": 1.0, "samples": 2, "excluded_null_r": 0},
        "totals": {
            "gross_pnl_usd": "1",
            "fees_usd": "0",
            "funding_usd": "0",
            "net_pnl_usd": "1",
        },
        "data_quality": "COMPLETE",
    }
    payload.update(overrides)
    return payload


def test_a_valid_defined_and_undefined_payload_validate() -> None:
    assert JournalStatsData.model_validate(_defined_stats()).status == "DEFINED"
    assert JournalStatsData.model_validate(_undefined_stats()).status == "UNDEFINED"


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"sample_size": 0}, "positive sample size"),
        ({"wins": 2}, "wins \\+ losses \\+ ties"),
        ({"win_rate": None}, "win rate and expectancy"),
        ({"avg_r": {"value": 1.0, "samples": 1, "excluded_null_r": 0}}, "average-R samples"),
    ],
)
def test_invalid_defined_combinations_are_rejected(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        JournalStatsData.model_validate(_defined_stats(**overrides))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"sample_size": 1, "wins": 1}, "zero sample and empty buckets"),
        ({"win_rate": 0.5}, "must not fabricate"),
        ({"avg_r": {"value": 1.0, "samples": 1, "excluded_null_r": 0}}, "average-R"),
        (
            {
                "totals": {
                    "gross_pnl_usd": "1",
                    "fees_usd": None,
                    "funding_usd": None,
                    "net_pnl_usd": None,
                }
            },
            "empty totals",
        ),
    ],
)
def test_invalid_undefined_combinations_are_rejected(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        JournalStatsData.model_validate(_undefined_stats(**overrides))


def test_an_unknown_status_or_quality_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JournalStatsData.model_validate(_defined_stats(status="MAYBE"))
    with pytest.raises(ValidationError):
        JournalStatsData.model_validate(_defined_stats(data_quality="STALE"))


def test_avg_r_counts_are_non_negative_and_value_tracks_samples() -> None:
    with pytest.raises(ValidationError):
        JournalAvgR.model_validate({"value": None, "samples": 0, "excluded_null_r": -1})
    with pytest.raises(ValidationError):
        JournalAvgR.model_validate({"value": None, "samples": 1, "excluded_null_r": 0})
    with pytest.raises(ValidationError):
        JournalAvgR.model_validate({"value": 1.0, "samples": 0, "excluded_null_r": 0})


def test_the_export_format_is_a_closed_literal() -> None:
    base = {
        "path": "/tmp/export.csv",
        "rows": 0,
        "bytes": 10,
        "neutralized_cells": 0,
        "skipped_unreadable": 0,
        "data_quality": "MISSING",
    }
    assert JournalExportData.model_validate({**base, "format": "csv"}).format == "csv"
    with pytest.raises(ValidationError):
        JournalExportData.model_validate({**base, "format": "parquet"})


def test_slippage_prices_are_decimal_strings_not_dynamic() -> None:
    payload = {
        "status": "OK",
        "entry_avg_price": "70000.00",
        "reference_price": "69900.00",
        "reference_as_of": "2026-11-14T11:00:00.000000Z",
        "bps": 14.3,
        "formula": "bps",
        "reasons": [],
    }
    validated = JournalSlippageData.model_validate(payload)
    assert validated.entry_avg_price == "70000.00"
    with pytest.raises(ValidationError):
        JournalSlippageData.model_validate({**payload, "entry_avg_price": 70000.0})


def test_the_comparison_statuses_are_closed_literals() -> None:
    zone = {
        "status": "INSIDE",
        "entry": "70000",
        "zone_low": "69000",
        "zone_high": "71000",
        "inside": True,
        "distance_bps": None,
        "reasons": [],
    }
    assert JournalZoneData.model_validate(zone).status == "INSIDE"
    with pytest.raises(ValidationError):
        JournalZoneData.model_validate({**zone, "status": "OK"})

    touch = {"kind": None, "at": None, "ambiguous": False, "status": "OK", "reasons": []}
    assert JournalFirstTouchData.model_validate(touch).status == "OK"
    with pytest.raises(ValidationError):
        JournalFirstTouchData.model_validate({**touch, "status": "INSIDE"})


def test_the_compare_quality_is_a_closed_literal() -> None:
    payload = {
        "trade_id": "TRD-1",
        "position_id": "POS-1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "opened_at": "2026-11-13T12:00:00.000Z",
        "closed_at": "2026-11-14T11:00:00.000Z",
        "net_pnl_usd": "10",
        "zone": {
            "status": "N/A",
            "entry": None,
            "zone_low": None,
            "zone_high": None,
            "inside": None,
            "distance_bps": None,
            "reasons": ["missing"],
        },
        "slippage": {
            "status": "N/A",
            "entry_avg_price": None,
            "reference_price": None,
            "reference_as_of": None,
            "bps": None,
            "formula": "bps",
            "reasons": ["missing"],
        },
        "first_touch": {
            "kind": None,
            "at": None,
            "ambiguous": False,
            "status": "N/A",
            "reasons": ["missing"],
        },
        "decision": {
            "status": "N/A",
            "decision": None,
            "direction_agrees": None,
            "matched": None,
            "net_pnl_usd": "10",
            "reasons": ["missing"],
            "decision_id": None,
        },
        "reasons": ["missing"],
        "data_quality": "MISSING",
    }
    assert JournalCompareData.model_validate(payload).data_quality == "MISSING"
    with pytest.raises(ValidationError):
        JournalCompareData.model_validate({**payload, "data_quality": "STALE"})
