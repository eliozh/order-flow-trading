"""Typed runtime settings loaded from ``OFT_*`` environment variables."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    )

    env: Environment = Environment.PAPER
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

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

    @field_validator("data_dir")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return value.expanduser()

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


def load_settings(**overrides: Any) -> Settings:
    """Load settings from the environment, applying explicit overrides last."""
    return Settings(**overrides)
