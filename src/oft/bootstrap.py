"""Composition root: the one place adapters are wired to application services."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from oft.adapters.archive import (
    FixtureArchiveSource,
    HttpsArchiveSource,
    UnconfiguredArchiveSource,
)
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.llm_anthropic import AnthropicAdapter
from oft.adapters.llm_deepseek import DeepSeekAdapter
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.llm_stub import LLMStub
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.llm_orchestrator import (
    LLMRuntimeRegistry,
    provider_api_key,
    set_default_adapter_factory,
)
from oft.application.rate_limiter import (
    GlobalRateLimiter,
    StructlogAlertSink,
    WsSubscriptionBudget,
)
from oft.application.status import StatusReport, build_status
from oft.application.symbol_registry import SymbolRegistry
from oft.clock import SystemClock
from oft.config import Settings, load_settings
from oft.ports.archive import ArchiveSourcePort
from oft.ports.clock import ClockPort
from oft.ports.llm import LLMPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort

PROVIDER_NAMES: tuple[str, ...] = ("openai", "anthropic", "deepseek", "stub")
LLM_ROLE_NAMES: tuple[str, ...] = ("ANALYSIS", "ENTRY_RECHECK", "MANAGEMENT", "REVIEW")


def build_llm_adapter(settings: Settings, name: str | None) -> LLMPort | None:
    """The provider-construction seam; tests monkeypatch this function."""
    normalized = (name or "").strip().lower()
    if normalized == "stub":
        return LLMStub(fixtures_dir=settings.llm_fixtures_dir)
    if normalized == "openai":
        return OpenAIAdapter(
            api_key=provider_api_key(settings, normalized),
            base_url=settings.llm_openai_base_url,
        )
    if normalized == "anthropic":
        return AnthropicAdapter(
            api_key=provider_api_key(settings, normalized),
            base_url=settings.llm_anthropic_base_url,
        )
    if normalized == "deepseek":
        return DeepSeekAdapter(
            api_key=provider_api_key(settings, normalized),
            base_url=settings.llm_deepseek_base_url,
        )
    return None


def _adapter_factory_seam(settings: Settings, name: str) -> LLMPort | None:
    """Late-bound seam so tests that monkeypatch ``build_llm_adapter`` win."""
    return build_llm_adapter(settings, name)


# The application layer resolves the probe factory through this registered seam
# instead of importing the composition root (the execution path forbids it).
set_default_adapter_factory(_adapter_factory_seam)


def resolve_llm_provider(settings: Settings, requested: str | None = None) -> LLMPort | None:
    """Resolve one provider name to an adapter through the construction seam.

    An unknown or missing name resolves to ``None`` so callers can report
    ``LLM_PROVIDER_NOT_CONFIGURED``.
    """
    name = (requested or settings.llm_provider or "").strip().lower()
    if not name:
        return None
    return build_llm_adapter(settings, name)


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
    scan_lock: threading.Lock
    rate_limiter: GlobalRateLimiter
    ws_budget: WsSubscriptionBudget
    llm_runtime: LLMRuntimeRegistry
    llm_provider: LLMPort | None = None

    def provider_for(self, requested: str | None = None) -> LLMPort | None:
        """Resolve the provider for a scan/entry request consistently.

        The cached adapter is returned only when the request names the
        container's configured provider; any other name (for example
        ``--provider stub`` while OpenAI is configured) is resolved by name so
        the request can never silently reach a different provider.
        """
        configured = (self.settings.llm_provider or "").strip().lower()
        name = (requested or "").strip().lower() or configured
        if not name:
            return None
        if name == configured and self.llm_provider is not None:
            return self.llm_provider
        return resolve_llm_provider(self.settings, requested)


def _seed_symbol_registry(registry: SymbolRegistry, settings: Settings) -> None:
    """Fixture-only registry seed for the walking stage (idempotent)."""
    if registry.count() > 0:
        return
    fixture = settings.exchange_info_fixture
    if fixture.is_file():
        registry.refresh_from_fixture(fixture)


def build_container(
    settings: Settings | None = None,
    *,
    initialize: bool = False,
    clock: ClockPort | None = None,
) -> AppContainer:
    """Wire adapters to ports.

    ``initialize=True`` is the daemon/writer path. Diagnostics leave it false
    so ``oft status`` never writes to the data directory. ``clock`` is injected
    for deterministic tests and replay (ADR-0011); production uses the system
    clock.
    """
    resolved = settings or load_settings()
    store = SqliteStore(resolved)
    parquet = ParquetStore(resolved)
    resolved_clock = clock or SystemClock()
    rate_limiter = GlobalRateLimiter(clock=resolved_clock, alert_sink=StructlogAlertSink())
    ws_budget = WsSubscriptionBudget()
    symbol_registry = SymbolRegistry(store, clock=resolved_clock)
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
        clock=resolved_clock,
        rate_limiter=rate_limiter,
    )
    if initialize:
        resolved.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # ``exist_ok=True`` does not tighten a pre-created directory.
        resolved.data_dir.chmod(0o700)
        store.initialize()
        parquet.ensure_layout()
        _seed_symbol_registry(symbol_registry, resolved)
    llm_provider = resolve_llm_provider(resolved)
    llm_runtime = LLMRuntimeRegistry(clock=resolved_clock, settings=resolved)
    return AppContainer(
        settings=resolved,
        clock=resolved_clock,
        market_data=FixtureMarketData(fixtures_dir=resolved.fixtures_dir),
        store=store,
        parquet=parquet,
        symbol_registry=symbol_registry,
        archive=archive,
        backfill=backfill,
        scan_lock=threading.Lock(),
        rate_limiter=rate_limiter,
        ws_budget=ws_budget,
        llm_runtime=llm_runtime,
        llm_provider=llm_provider,
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
