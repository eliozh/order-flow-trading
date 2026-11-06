"""exchangeInfo fixture ingest into the SQLite symbol registry."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.binance_rest import BinanceRestClient, HttpResponse
from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.application.symbol_registry import (
    ExchangeInfoParseError,
    SymbolRegistry,
)
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "exchange_info_sample.json"
FIRST = datetime(2026, 11, 6, 4, 0, 0, tzinfo=UTC)
SECOND = datetime(2026, 11, 6, 5, 30, 0, tzinfo=UTC)


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _store(settings: Settings) -> SqliteStore:
    store = SqliteStore(settings)
    store.initialize()
    return store


def _registry(store: SqliteStore, instant: datetime) -> SymbolRegistry:
    return SymbolRegistry(store, clock=FixedClock(instant))


def _row(settings: Settings, symbol: str) -> sqlite3.Row:
    connection = sqlite3.connect(database_path(settings))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row


def _count(settings: Settings) -> int:
    connection = sqlite3.connect(database_path(settings))
    try:
        row = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()
    finally:
        connection.close()
    return int(row[0])


def test_fixture_ingest_persists_the_expected_specs(settings: Settings) -> None:
    store = _store(settings)
    registry = _registry(store, FIRST)

    result = registry.refresh_from_fixture(FIXTURE)

    assert (result.inserted, result.updated, result.unchanged) == (3, 0, 0)
    contracts = {contract.symbol: contract for contract in registry.list_symbols()}
    assert set(contracts) == {"BTCUSDT", "ETHUSDT", "XAUUSDT"}

    btc = contracts["BTCUSDT"]
    assert btc.tick_size == Decimal("0.10")
    assert str(btc.step_size) == "0.001"
    assert str(btc.min_qty) == "0.001"
    assert str(btc.min_notional) == "5.00"
    assert btc.price_precision == 2
    assert btc.quantity_precision == 3
    assert btc.contract_type == "PERPETUAL"
    assert btc.underlying_type == "COIN"
    assert btc.onboard_date == 1567900800000
    assert btc.listed_date == "2019-09-08"

    xau = contracts["XAUUSDT"]
    assert xau.contract_type == "TRADIFI_PERPETUAL"
    assert xau.tradifi is True
    assert xau.underlying_type == "COMMODITY"
    assert xau.listed_date == "2025-12-11"

    row = _row(settings, "BTCUSDT")
    assert row["updated_at"] == "2026-11-06T04:00:00.000Z"


def test_reingest_is_idempotent_and_does_not_touch_updated_at(settings: Settings) -> None:
    store = _store(settings)
    _registry(store, FIRST).refresh_from_fixture(FIXTURE)

    result = _registry(store, SECOND).refresh_from_fixture(FIXTURE)

    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 3)
    assert _count(settings) == 3
    assert _row(settings, "BTCUSDT")["updated_at"] == "2026-11-06T04:00:00.000Z"


def test_a_changed_spec_updates_the_row_and_updated_at(settings: Settings) -> None:
    store = _store(settings)
    _registry(store, FIRST).refresh_from_fixture(FIXTURE)

    payload = _payload()
    for entry in payload["symbols"]:
        if isinstance(entry, dict) and entry.get("symbol") == "BTCUSDT":
            entry["filters"][0]["tickSize"] = "0.50"

    result = _registry(store, SECOND).refresh_from_payload(payload)

    assert (result.inserted, result.updated, result.unchanged) == (0, 1, 2)
    assert _count(settings) == 3
    row = _row(settings, "BTCUSDT")
    assert row["tick_size"] == "0.50"
    assert row["updated_at"] == "2026-11-06T05:30:00.000Z"
    assert _row(settings, "ETHUSDT")["updated_at"] == "2026-11-06T04:00:00.000Z"


def test_malformed_payload_is_rejected_without_writing(settings: Settings) -> None:
    store = _store(settings)
    registry = _registry(store, FIRST)

    with pytest.raises(ExchangeInfoParseError):
        registry.refresh_from_payload({"symbols": "nope"})

    assert registry.list_symbols() == ()


def test_registry_accepts_an_adapter_response(settings: Settings) -> None:
    store = _store(settings)
    registry = _registry(store, FIRST)

    def fake_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        return HttpResponse(status=200, headers={}, body=FIXTURE.read_bytes())

    result = asyncio.run(registry.refresh_from_exchange_info(BinanceRestClient(fetch=fake_fetch)))

    assert result.inserted == 3
    assert registry.get_symbol("XAUUSDT") is not None


def test_group_counts_split_crypto_and_tradfi(settings: Settings) -> None:
    registry = _registry(_store(settings), FIRST)
    registry.refresh_from_fixture(FIXTURE)

    assert registry.group_counts() == {"crypto": 2, "tradfi": 1, "total": 3}


def test_unknown_symbol_returns_none(settings: Settings) -> None:
    registry = _registry(_store(settings), FIRST)
    registry.refresh_from_fixture(FIXTURE)

    assert registry.get_symbol("NOPEUSDT") is None


def test_read_only_store_without_a_database_returns_empty(settings: Settings) -> None:
    store = SqliteStore(settings)

    assert store.list_symbols() == ()
    assert store.get_symbol("BTCUSDT") is None


def test_container_seeds_the_registry_from_the_committed_fixture(settings: Settings) -> None:
    container = build_container(settings, initialize=True)

    contracts = {contract.symbol: contract for contract in container.symbol_registry.list_symbols()}

    assert set(contracts) == {"BTCUSDT", "ETHUSDT", "XAUUSDT"}
    assert contracts["BTCUSDT"].tick_size == Decimal("0.10")


def test_fixture_file_matches_checksum() -> None:
    import hashlib

    checksums = (FIXTURE.parent / "CHECKSUMS").read_text(encoding="utf-8").split()
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()

    assert checksums[0] == digest
    assert checksums[1] == FIXTURE.name


@pytest.mark.skipif(
    os.environ.get("OFT_BINANCE_LIVE_SMOKE") != "1",
    reason="live exchangeInfo smoke is opt-in via OFT_BINANCE_LIVE_SMOKE=1",
)
def test_opt_in_live_exchange_info_smoke(settings: Settings) -> None:
    registry = _registry(_store(settings), FIRST)

    result = asyncio.run(registry.refresh_from_exchange_info(BinanceRestClient()))

    assert result.inserted > 0
    assert registry.get_symbol("BTCUSDT") is not None
