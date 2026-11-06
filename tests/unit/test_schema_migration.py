"""Schema v1 -> v2 migration adds CHECK constraints to an existing symbols table."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.config import Settings

_V1_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-11-06T00:00:00.000Z');
CREATE TABLE symbols (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    underlying_type TEXT,
    status TEXT NOT NULL,
    tick_size TEXT NOT NULL,
    step_size TEXT NOT NULL,
    min_qty TEXT NOT NULL,
    min_notional TEXT NOT NULL,
    price_precision INTEGER NOT NULL,
    quantity_precision INTEGER NOT NULL,
    onboard_date INTEGER NOT NULL,
    tradifi INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO symbols VALUES (
    'BTCUSDT', 'BTC', 'USDT', 'PERPETUAL', 'COIN', 'TRADING',
    '0.10', '0.001', '0.001', '5.00', 2, 3, 1567900800000, 0,
    '2026-11-06T04:00:00.000Z'
);
"""


def _v1_settings(tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.executescript(_V1_SCHEMA)
        connection.commit()
    finally:
        connection.close()
    return settings


def test_v1_database_upgrades_to_v2_with_effective_checks(tmp_path: Path) -> None:
    settings = _v1_settings(tmp_path)
    store = SqliteStore(settings)

    store.initialize()
    store.initialize()  # idempotent

    assert store.get_symbol("BTCUSDT") is not None
    connection = sqlite3.connect(database_path(settings))
    try:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'symbols'"
        ).fetchone()[0]
        assert "CHECK" in ddl
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO symbols VALUES (
                    'BADUSDT', 'B', 'USDT', 'PERPETUAL', NULL, 'TRADING',
                    '0.1', '0.1', '1', '5', -1, 2, 1567900800000, 0, 't'
                )
                """
            )
    finally:
        connection.close()

    assert versions == {1, 2}
