"""Composition root: the one place adapters are wired to application services."""

from __future__ import annotations

from dataclasses import dataclass

from oft.adapters.archive import (
    FixtureArchiveSource,
    HttpsArchiveSource,
    UnconfiguredArchiveSource,
)
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.status import StatusReport, build_status
from oft.application.symbol_registry import SymbolRegistry
from oft.clock import SystemClock
from oft.config import Settings, load_settings
from oft.ports.archive import ArchiveSourcePort
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
    symbol_registry: SymbolRegistry
    archive: ArchiveSourcePort
    backfill: BackfillService


def _seed_symbol_registry(registry: SymbolRegistry, settings: Settings) -> None:
    """Fixture-only registry seed for the walking stage (idempotent)."""
    if registry.count() > 0:
        return
    fixture = settings.exchange_info_fixture
    if fixture.is_file():
        registry.refresh_from_fixture(fixture)


def build_container(settings: Settings | None = None, *, initialize: bool = False) -> AppContainer:
    """Wire adapters to ports.

    ``initialize=True`` is the daemon/writer path. Diagnostics leave it false
    so ``oft status`` never writes to the data directory.
    """
    resolved = settings or load_settings()
    store = SqliteStore(resolved)
    parquet = ParquetStore(resolved)
    clock = SystemClock()
    symbol_registry = SymbolRegistry(store, clock=clock)
    if resolved.archive_fixture_dir is not None:
        archive: ArchiveSourcePort = FixtureArchiveSource(resolved.archive_fixture_dir)
    elif resolved.env == "test":
        # Never fall back to the network in a test environment.
        archive = UnconfiguredArchiveSource(
            "OFT_ENV=test requires OFT_ARCHIVE_FIXTURE_DIR; "
            "HTTPS archive access is disabled in tests"
        )
    else:
        archive = HttpsArchiveSource()
    backfill = BackfillService(
        settings=resolved,
        archive=archive,
        parquet=parquet,
        store=store,
        clock=clock,
    )
    if initialize:
        resolved.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # ``exist_ok=True`` does not tighten a pre-created directory.
        resolved.data_dir.chmod(0o700)
        store.initialize()
        parquet.ensure_layout()
        _seed_symbol_registry(symbol_registry, resolved)
    return AppContainer(
        settings=resolved,
        clock=clock,
        market_data=FixtureMarketData(fixtures_dir=resolved.fixtures_dir),
        store=store,
        parquet=parquet,
        symbol_registry=symbol_registry,
        archive=archive,
        backfill=backfill,
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
