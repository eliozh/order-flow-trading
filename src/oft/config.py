"""Typed runtime settings loaded from ``OFT_*`` environment variables."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7611
CONFIG_INVALID = "CONFIG_INVALID"
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


class Settings(BaseSettings):
    """Application settings.

    Safe defaults: paper environment, loopback binding, and no credentials.
    Credentials are optional and default to ``None``; the walking skeleton
    never reads them.
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

    # Paper broker defaults (ADR-0006). Decimal strings, never floats.
    paper_initial_balance: str = "10000.00"
    paper_slippage_bps: str = "2"
    paper_taker_fee_bps: str = "5"

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
