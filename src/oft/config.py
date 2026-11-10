"""Typed runtime settings loaded from ``OFT_*`` environment variables."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from oft.domain.pricing import PriceTable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7611
CONFIG_INVALID = "CONFIG_INVALID"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "market"
DEFAULT_KLINES_FIXTURE = DEFAULT_FIXTURES_DIR / "btcusdt_klines_4h.csv"
DEFAULT_EXCHANGE_INFO_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "binance"
    / "exchange_info_sample.json"
)
DEFAULT_LLM_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"


class Environment(StrEnum):
    """Supported execution environments."""

    PAPER = "paper"
    LIVE = "live"
    TEST = "test"


_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# A key may only be sent to the provider's official host or loopback unless the
# operator opts into a custom host (S8).
_OFFICIAL_LLM_HOSTS: dict[str, frozenset[str]] = {
    "llm_openai_base_url": frozenset({"api.openai.com"}),
    "llm_anthropic_base_url": frozenset({"api.anthropic.com"}),
    "llm_deepseek_base_url": frozenset({"api.deepseek.com"}),
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class Settings(BaseSettings):
    """Application settings.

    Safe defaults: paper environment, loopback binding, and no credentials.
    Credentials are optional and default to ``None``; the LLM path reads
    provider keys in this build.
    """

    model_config = SettingsConfigDict(
        env_prefix="OFT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: Environment = Environment.PAPER
    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    data_dir: Path = Field(
        default=Path("./data"),
        validation_alias=AliasChoices("OFT_DATA_DIR", "OFT_DATA"),
    )
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR
    klines_fixture: Path = DEFAULT_KLINES_FIXTURE
    exchange_info_fixture: Path = DEFAULT_EXCHANGE_INFO_FIXTURE
    archive_fixture_dir: Path | None = None
    allow_fixture_scan_debug: bool = False
    log_level: str = "INFO"

    # LLM configuration. No default provider/model is shipped (plan §2.2).
    llm_provider: str | None = None
    llm_model: str = "fixture"
    llm_fixtures_dir: Path = DEFAULT_LLM_FIXTURES_DIR
    llm_model_analysis: str | None = None
    llm_model_entry_recheck: str | None = None
    llm_model_management: str | None = None
    llm_model_review: str | None = None
    # Only the official provider hosts (plus loopback) are accepted unless the
    # operator explicitly opts into a custom host (S8). Declared before the
    # base-URL fields so the validator can read it from ``info.data``.
    llm_allow_custom_base_url: bool = False
    llm_openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    llm_anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    llm_deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    llm_price_table: Path | None = None
    llm_max_concurrency: int = Field(default=2, ge=1)
    llm_max_concurrency_deepseek: int = Field(default=1, ge=1)

    # Budget thresholds (ADR-0005 defaults; operator rows override at runtime).
    budget_daily_usd: str = "10.00"
    budget_weekly_usd: str = "50.00"
    budget_monthly_usd: str = "150.00"
    budget_warn_pct: float = Field(default=80.0, gt=0, le=100)

    # Paper broker defaults (ADR-0006). Decimal strings, never floats.
    paper_initial_balance: str = "10000.00"
    paper_slippage_bps: str = "2"
    paper_taker_fee_bps: str = "5"

    # Trigger evaluation cadence (ADR-0007: 15 s default, 5-60 s configurable).
    trigger_interval_seconds: int = Field(default=15, ge=5, le=60)
    trigger_stale_after_seconds: int = Field(default=60, ge=1)

    api_token: SecretStr | None = None
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return level

    @field_validator(
        "data_dir",
        "fixtures_dir",
        "klines_fixture",
        "exchange_info_fixture",
        "llm_fixtures_dir",
    )
    @classmethod
    def _expand_paths(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("llm_provider")
    @classmethod
    def _normalize_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None

    @field_validator(
        "llm_model_analysis",
        "llm_model_entry_recheck",
        "llm_model_management",
        "llm_model_review",
    )
    @classmethod
    def _normalize_optional_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("llm_price_table", mode="before")
    @classmethod
    def _empty_price_table_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, Path) and not str(value).strip():
            return None
        return value

    @field_validator("llm_price_table")
    @classmethod
    def _expand_optional_price_table(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator("budget_daily_usd", "budget_weekly_usd", "budget_monthly_usd")
    @classmethod
    def _positive_decimal_threshold(cls, value: str) -> str:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("budget thresholds must be decimal strings") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("budget thresholds must be positive decimals")
        return value

    @field_validator("llm_openai_base_url", "llm_anthropic_base_url", "llm_deepseek_base_url")
    @classmethod
    def _validate_base_url(cls, value: str, info: ValidationInfo) -> str:
        """Reject a base URL that could exfiltrate the key (M18/S8)."""
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("LLM base URLs must not be empty")
        parts = urlsplit(normalized)
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("LLM base URLs must include a host")
        if parts.username or parts.password:
            raise ValueError("LLM base URLs must not contain userinfo")
        if parts.query or parts.fragment:
            raise ValueError("LLM base URLs must not contain a query or fragment")
        scheme = parts.scheme.lower()
        env = info.data.get("env")
        in_test = env in (Environment.TEST, "test")
        if scheme == "http" and (host in _LOOPBACK_HOSTS or in_test):
            return normalized
        if scheme != "https":
            raise ValueError(
                "LLM base URLs must use https; http is allowed only for loopback or OFT_ENV=test"
            )
        official = _OFFICIAL_LLM_HOSTS.get(info.field_name or "", frozenset())
        if host in official or host in _LOOPBACK_HOSTS or in_test:
            return normalized
        if bool(info.data.get("llm_allow_custom_base_url")):
            return normalized
        raise ValueError(
            "LLM base URLs must use an official provider host or loopback; "
            "set OFT_LLM_ALLOW_CUSTOM_BASE_URL=true to allow a custom host"
        )

    def model_for_role(self, role: str) -> str | None:
        """Resolve the configured model for one ``LLMRole`` (no default model)."""
        return self.model_for_role_map().get(role.strip().upper())

    def model_for_role_map(self) -> dict[str, str | None]:
        return {
            "ANALYSIS": self.llm_model_analysis,
            "ENTRY_RECHECK": self.llm_model_entry_recheck,
            "MANAGEMENT": self.llm_model_management,
            "REVIEW": self.llm_model_review,
        }

    def price_table(self) -> PriceTable:
        """Load the operator price table; ``None`` yields the honest empty table."""
        from oft.domain.pricing import load_price_table

        return load_price_table(self.llm_price_table)

    @model_validator(mode="after")
    def _validate_price_table(self) -> Settings:
        """An unreadable/inconsistent operator price table is ``CONFIG_INVALID``."""
        if self.llm_price_table is not None:
            from oft.domain.pricing import PriceTableError, load_price_table

            try:
                load_price_table(self.llm_price_table)
            except PriceTableError as exc:
                raise ValueError(exc.message) from exc
        return self

    @field_validator("archive_fixture_dir")
    @classmethod
    def _expand_optional_path(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator(
        "api_token",
        "binance_api_key",
        "binance_api_secret",
        "openai_api_key",
        "anthropic_api_key",
        "deepseek_api_key",
        "telegram_bot_token",
    )
    @classmethod
    def _empty_secret_is_unset(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            return None
        return value


class ConfigError(Exception):
    """Configuration failed validation; surfaced as ``CONFIG_INVALID``."""

    code = CONFIG_INVALID

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def load_settings(**overrides: Any) -> Settings:
    """Load settings from the environment, applying explicit overrides last.

    Invalid settings are wrapped so every entry point can report the stable
    ``CONFIG_INVALID`` code instead of leaking a traceback.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
