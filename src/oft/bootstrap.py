"""Composition root: the one place adapters are wired to application services."""

from __future__ import annotations

import hashlib
import json
import logging
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
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.adapters.telegram_fake import TelegramFake
from oft.application.backfill_service import BackfillService
from oft.application.llm_orchestrator import (
    LLMRuntimeRegistry,
    provider_api_key,
    set_default_adapter_factory,
)
from oft.application.notifications import NotificationService
from oft.application.rate_limiter import (
    GlobalRateLimiter,
    StructlogAlertSink,
    WsSubscriptionBudget,
)
from oft.application.secrets_service import SecretsService, SecretStoreUnavailable
from oft.application.settings_service import SettingsService, resolve_effective_llm_settings
from oft.application.status import StatusReport, build_status
from oft.application.symbol_registry import SymbolRegistry
from oft.application.telegram_service import TelegramService
from oft.clock import SystemClock
from oft.config import ConfigError, Settings, load_settings
from oft.domain.settings import SettingValidationError
from oft.logging import set_redaction_values
from oft.ports.archive import ArchiveSourcePort
from oft.ports.clock import ClockPort
from oft.ports.llm import LLMPort
from oft.ports.market_data import MarketDataPort
from oft.ports.store import StorePort
from oft.ports.telegram import TelegramPort

PROVIDER_NAMES: tuple[str, ...] = ("openai", "anthropic", "deepseek", "stub")
LLM_ROLE_NAMES: tuple[str, ...] = ("ANALYSIS", "ENTRY_RECHECK", "MANAGEMENT", "REVIEW")

# The runtime LLM state is one immutable ``Settings`` snapshot swapped under
# this lock. Readers take the attribute once, so they can never observe a new
# provider with the old key or base URL.
_LLM_REFRESH_LOCK = threading.RLock()


def _adapter_signature(settings: Settings) -> str:
    """A non-reversible fingerprint of the fields an adapter is built from.

    Used only to decide whether the cached adapter is still current; a rotated
    key or a changed base URL changes the signature and forces a rebuild.
    """
    keys: dict[str, str | None] = {}
    for provider in PROVIDER_NAMES:
        if provider == "stub":
            continue
        attribute = {
            "openai": "openai_api_key",
            "anthropic": "anthropic_api_key",
            "deepseek": "deepseek_api_key",
        }[provider]
        secret = getattr(settings, attribute, None)
        getter = getattr(secret, "get_secret_value", None)
        value = getter() if callable(getter) else secret
        keys[provider] = (
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            if isinstance(value, str) and value
            else None
        )
    material = {
        "provider": settings.llm_provider,
        "base_urls": {
            "openai": settings.llm_openai_base_url,
            "anthropic": settings.llm_anthropic_base_url,
            "deepseek": settings.llm_deepseek_base_url,
        },
        "keys": keys,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


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
    # Pristine pre-overlay settings: runtime refreshes always resolve from this
    # copy, never from the already-overlaid mutable ``settings`` instance.
    llm_base_settings: Settings | None = None
    # The adapter signature at build time. A cached adapter is reused only when
    # the requested provider and the effective key/base URLs still match, so a
    # rotation or a provider change rebuilds without a restart.
    llm_adapter_signature: str | None = None
    # Slice 47: the Telegram port/service. Defaults keep existing construction
    # sites and tests working; ``build_container`` always supplies both.
    telegram_port: TelegramPort | None = None
    telegram_service: TelegramService | None = None
    # Slice 49: the one push path. Default ``None`` keeps existing construction
    # sites valid; ``build_container`` always supplies it.
    notifications: NotificationService | None = None

    def refresh_llm_settings(self) -> Settings:
        """Re-resolve the persisted LLM overlay and swap the runtime snapshot.

        Called at provider-resolution time so a save through the API or CLI is
        visible to the very next scan/entry/management/review/probe call. The
        whole snapshot is assigned at once: no reader can see a torn mixture of
        a new provider and an old key or base URL.
        """
        base = self.llm_base_settings or self.settings
        try:
            effective = resolve_effective_llm_settings(
                settings=base, store=self.store, clock=self.clock
            )
        except (SettingValidationError, SecretStoreUnavailable, ConfigError) as exc:
            # Every CLI/daemon/API path refreshes through here; an invalid
            # environment value or unavailable store must be a typed
            # ``CONFIG_INVALID``, never an untyped traceback.
            message = getattr(exc, "message", None) or str(exc)
            raise ConfigError(message) from exc
        with _LLM_REFRESH_LOCK:
            object.__setattr__(self, "settings", effective)
        self.llm_runtime.update_settings(effective)
        return effective

    def provider_for(
        self, requested: str | None = None, *, effective: Settings | None = None
    ) -> LLMPort | None:
        """Resolve the provider for a scan/entry request from effective settings.

        The requested name (or the active provider) is resolved against the
        freshly resolved overlay, so a rotated key, a changed base URL, or a
        newly configured provider applies without a restart. An unknown name
        resolves to ``None``; it never falls back to another provider's key.
        A rebuilt adapter for the configured provider is cached under its
        signature, so repeated calls do not reconstruct it.

        ``effective`` lets a batch/request that already refreshed once pass its
        single snapshot, so per-symbol resolution does not re-read the config,
        secret metadata, or OS keyring.
        """
        if effective is None:
            effective = self.refresh_llm_settings()
        configured = (effective.llm_provider or "").strip().lower()
        name = (requested or "").strip().lower() or configured
        if not name:
            return None
        signature = _adapter_signature(effective)
        if name == configured and self.llm_provider is not None:
            with _LLM_REFRESH_LOCK:
                if self.llm_adapter_signature == signature:
                    # An injected/replay source or an unchanged built adapter is current.
                    return self.llm_provider
        built = resolve_llm_provider(effective, name)
        if built is not None and name == configured:
            with _LLM_REFRESH_LOCK:
                object.__setattr__(self, "llm_provider", built)
                object.__setattr__(self, "llm_adapter_signature", signature)
        return built


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
    # Snapshot the environment-only settings before the persisted overlay is
    # applied: runtime refreshes must never treat an already-overlaid value as
    # the environment base (a deleted key would otherwise appear to persist).
    llm_base_settings = resolved.model_copy(deep=True)
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
    # Resolve the persisted LLM overlay before any caller reads settings, so the
    # CLI and a freshly started daemon agree on the effective provider/models.
    try:
        effective = resolve_effective_llm_settings(
            settings=llm_base_settings, store=store, clock=resolved_clock
        )
        llm_provider = resolve_llm_provider(effective)
    except (SettingValidationError, SecretStoreUnavailable, ConfigError) as exc:
        # A bad environment provider or a key whose store is unavailable is a
        # configuration error the API/CLI report as ``CONFIG_INVALID``.
        message = getattr(exc, "message", None) or str(exc)
        raise ConfigError(message) from exc
    llm_runtime = LLMRuntimeRegistry(clock=resolved_clock, settings=effective)
    market_data = FixtureMarketData(fixtures_dir=resolved.fixtures_dir)
    # Telegram: the fake in tests (deterministic, network-free) and a lazy Bot
    # API adapter otherwise. The adapter resolves the token per call from the
    # pristine secret service, so a missing token never blocks startup and a
    # rotation applies without a restart.
    telegram_secrets = SecretsService(store=store, settings=llm_base_settings, clock=resolved_clock)
    # Value-based log redaction: a stored/env Telegram token outside the
    # canonical shape must still be scrubbed from stdlib records. Resolve only
    # the Telegram token here: eagerly reading every provider key on each CLI
    # command would hit the OS keyring / the scrypt KDF. A store failure must
    # never block startup; the unbounded token-shape scrub still catches a
    # canonical token without the registry, and a runtime rotation is picked up
    # on the next container build.
    try:
        set_redaction_values(telegram_secrets.redaction_values(include_providers=False))
    except Exception:  # noqa: BLE001 - best-effort defense in depth
        logging.getLogger(__name__).warning("telegram_redaction_values_unavailable")
    telegram_settings_service = SettingsService(
        store=store, clock=resolved_clock, settings=llm_base_settings
    )
    telegram_port: TelegramPort
    if resolved.env == "test":
        telegram_port = TelegramFake()
    else:
        telegram_port = TelegramBotApiAdapter(
            token_resolver=lambda: telegram_secrets.resolve_secret("telegram", "bot_token")
        )
    telegram_service = TelegramService(
        settings=effective,
        store=store,
        clock=resolved_clock,
        market_data=market_data,
        secrets=telegram_secrets,
        settings_service=telegram_settings_service,
        port=telegram_port,
    )
    notifications = NotificationService(
        store=store,
        clock=resolved_clock,
        channel=telegram_service,
        settings_service=telegram_settings_service,
    )
    return AppContainer(
        settings=effective,
        clock=resolved_clock,
        market_data=market_data,
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
        llm_base_settings=llm_base_settings,
        llm_adapter_signature=_adapter_signature(effective),
        telegram_port=telegram_port,
        telegram_service=telegram_service,
        notifications=notifications,
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
