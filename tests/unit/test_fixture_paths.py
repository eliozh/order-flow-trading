"""Fixture path boundaries: reads stay inside the configured fixtures directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from oft.adapters.fixture_market_data import FixtureMarketData, FixturePathForbidden


def test_fixture_adapter_rejects_paths_outside_the_fixtures_root(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("agg_trade_id,price\n", encoding="utf-8")
    adapter = FixtureMarketData(fixtures_dir=root)

    with pytest.raises(FixturePathForbidden) as excinfo:
        adapter.load_agg_trades(outside, "BTCUSDT")

    assert excinfo.value.code == "VALIDATION_FAILED"


def test_fixture_adapter_rejects_traversal_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("agg_trade_id,price\n", encoding="utf-8")
    adapter = FixtureMarketData(fixtures_dir=root)

    with pytest.raises(FixturePathForbidden):
        adapter.load_agg_trades(root / ".." / "outside.csv", "BTCUSDT")
