"""Starred symbols: idempotent store mutation and filtering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from oft.application.symbol_registry import SymbolRegistry
from oft.bootstrap import build_container
from oft.config import Settings
from oft.domain.time import utc_timestamp

NOW = utc_timestamp(datetime(2026, 11, 7, tzinfo=UTC))


def _registry(tmp_path: Path) -> SymbolRegistry:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    return container.symbol_registry


def test_star_unstar_is_idempotent_and_filters(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.set_starred("BTCUSDT", True, starred_at=NOW) is True
    assert registry.set_starred("BTCUSDT", True, starred_at=NOW) is True
    assert [item.symbol for item in registry.list_starred()] == ["BTCUSDT"]
    assert [item.symbol for item in registry.list_symbols(starred=True)] == ["BTCUSDT"]

    assert registry.set_starred("BTCUSDT", False) is True
    assert registry.list_starred() == ()
    assert registry.list_symbols(starred=True) == ()
    assert registry.list_symbols(starred=False) != ()


def test_unknown_symbol_is_not_starred(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.set_starred("NOPEUSDT", True) is False
    assert registry.list_starred() == ()


def test_star_survives_a_registry_refresh(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(settings, initialize=True)
    registry = container.symbol_registry
    assert registry.set_starred("BTCUSDT", True, starred_at=NOW) is True

    registry.refresh_from_fixture(settings.exchange_info_fixture)

    assert [item.symbol for item in registry.list_starred()] == ["BTCUSDT"]
