"""Composition root: the one place adapters are wired to application services."""

from __future__ import annotations

from dataclasses import dataclass

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.status import StatusReport, build_status
from oft.clock import SystemClock
from oft.config import Settings, load_settings
from oft.ports.clock import ClockPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort


@dataclass(frozen=True)
class AppContainer:
    settings: Settings
    clock: ClockPort
    market_data: MarketDataPort
    store: StorePort
    parquet: ParquetStore


def build_container(settings: Settings | None = None, *, initialize: bool = False) -> AppContainer:
    """Wire adapters to ports.

    ``initialize=True`` is the daemon/writer path. Diagnostics leave it false
    so ``oft status`` never writes to the data directory.
    """
    resolved = settings or load_settings()
    store = SqliteStore(resolved)
    parquet = ParquetStore(resolved)
    if initialize:
        resolved.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.initialize()
        parquet.ensure_layout()
    return AppContainer(
        settings=resolved,
        clock=SystemClock(),
        market_data=FixtureMarketData(fixtures_dir=resolved.fixtures_dir),
        store=store,
        parquet=parquet,
    )


def get_status(settings: Settings | None = None) -> StatusReport:
    """Build a status report through the same path for every client."""
    container = build_container(settings)
    return build_status(
        container.settings,
        container.market_data,
        container.clock,
        store=container.store,
    )
