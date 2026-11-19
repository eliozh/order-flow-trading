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

from oft.domain.llm_settings import MAX_CONCURRENCY
from oft.domain.settings import SCAN_PERIOD_HOURS, SUPPORTED_SCAN_PERIODS

if TYPE_CHECKING:
    from oft.domain.pricing import PriceTable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7611
CONFIG_INVALID = "CONFIG_INVALID"
SCAN_PERIOD_ENV = "OFT_SCAN_PERIOD"
SCAN_JITTER_ENV = "OFT_SCAN_JITTER_SECONDS"
DEFAULT_SCAN_PERIOD = "4h"
DEFAULT_SCAN_JITTER_SECONDS = 30
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

# The canonical secret-field list shared by config, doctor, and prompt redaction.
SECRET_FIELDS: tuple[str, ...] = (
    "api_token",
    "binance_api_key",
    "binance_api_secret",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "telegram_bot_token",
)

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
    llm_max_concurrency: int = Field(default=2, ge=1, le=MAX_CONCURRENCY)
    llm_max_concurrency_deepseek: int = Field(default=1, ge=1, le=MAX_CONCURRENCY)

    # Budget thresholds (ADR-0005 defaults; operator rows override at runtime).
    budget_daily_usd: str = "10.00"
    budget_weekly_usd: str = "50.00"
    budget_monthly_usd: str = "150.00"
    budget_warn_pct: float = Field(default=80.0, gt=0, le=100)

    # Paper broker defaults (ADR-0006). Decimal strings, never floats.
    paper_initial_balance: str = "10000.00"
    paper_slippage_bps: str = "2"
    paper_taker_fee_bps: str = "5"

    # Percentage-based risk caps (ADR-0006, slice 18). These are the ``config``
    # source; an operator row in ``system_state.risk_caps`` takes precedence.
    risk_per_trade_pct: float = Field(default=0.8, gt=0, le=100, allow_inf_nan=False)
    risk_max_concurrent_positions: int = Field(default=3, ge=1)
    risk_daily_loss_pct: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)
    risk_max_leverage: float = Field(default=5.0, gt=0, le=125, allow_inf_nan=False)
    risk_max_notional_pct: float = Field(default=20.0, gt=0, le=100, allow_inf_nan=False)
    risk_max_slippage_bps: float = Field(default=25.0, gt=0, le=10000, allow_inf_nan=False)
    risk_max_spread_bps: float = Field(default=15.0, gt=0, le=10000, allow_inf_nan=False)
    risk_stale_mark_seconds: int = Field(default=60, ge=1)

    # Scan cadence (ADR-0007). ``scan_period`` must be a supported UTC cadence
    # and ``scan_jitter_seconds`` must be non-negative and shorter than the
    # period. The pair is enforced by ``validate_scan_*`` when a consumer uses
    # it (strict ``load_settings``, ``daemon start``, and the scheduler) so
    # diagnostics can still overlay a persisted period (M3).
    scan_period: str = DEFAULT_SCAN_PERIOD
    scan_jitter_seconds: int = DEFAULT_SCAN_JITTER_SECONDS

    # Trigger evaluation cadence (ADR-0007: 15 s default, 5-60 s configurable).
    trigger_interval_seconds: int = Field(default=15, ge=5, le=60)
    trigger_stale_after_seconds: int = Field(default=60, ge=1)

    # Position monitor cadence (code-only protective/stale tick) and the
    # management cadence. ``management_cadence`` overrides the scan period;
    # ``None`` follows the active scan period (ADR-0007).
    position_monitor_interval_seconds: int = Field(default=15, ge=5, le=60)
    management_cadence: str | None = None
    management_funding_settlement_enabled: bool = True
    management_risk_breach_enabled: bool = True

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

    @field_validator("scan_period")
    @classmethod
    def _strip_scan_period(cls, value: str) -> str:
        """Normalize surrounding whitespace; membership is checked at load.

        Membership is deliberately not enforced here: diagnostics resolve a
        persisted ``scan.period`` before the environment value (M3), so an
        invalid ``OFT_SCAN_PERIOD`` must not stop ``oft daemon status`` from
        reporting a valid saved period. ``load_settings`` raises
        ``CONFIG_INVALID`` when the value is actually used, and the effective
        phase (overlay -> ``ScheduleState``) validates it.
        """
        return value.strip()

    @field_validator("scan_jitter_seconds", mode="before")
    @classmethod
    def _blank_scan_jitter_is_default(cls, value: object) -> object:
        """Treat a blank ``OFT_SCAN_JITTER_SECONDS`` as the default value.

        ``.env.example`` documents blank-to-default for optional values.
        ``oft doctor`` never resolves the cadence, so it only needs the load to
        succeed; ``oft status`` does resolve it through ``_schedule_status`` and
        degrades to a missing schedule on error. Non-blank non-integers still
        fail at the typed parse; an out-of-range integer still fails when the
        cadence is validated.
        """
        if isinstance(value, str) and not value.strip():
            return DEFAULT_SCAN_JITTER_SECONDS
        return value

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
        if not normalized:
            return None
        # ``stub`` is the environment-only test transport; the real providers
        # are the persisted vocabulary. An unknown value is a startup error, not
        # a late ``LLMSettingsValidationError`` traceback from composition.
        if normalized not in {"openai", "anthropic", "deepseek", "stub"}:
            raise ValueError(
                "llm_provider must be one of: openai, anthropic, deepseek, stub; "
                f"got {normalized!r}"
            )
        return normalized

    @field_validator("management_cadence")
    @classmethod
    def _normalize_management_cadence(cls, value: str | None) -> str | None:
        """Validate an explicit management cadence against the scan periods.

        Kept as a small local check (not an import of the scheduler) so
        ``config`` remains a leaf module for the import-linter adapter contract;
        the values mirror ``oft.application.scheduler.SCAN_PERIODS``.
        """
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized not in {"4h", "8h"}:
            raise ValueError(f"management_cadence must be one of ('4h', '8h'), got {normalized!r}")
        return normalized

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

    @field_validator("paper_slippage_bps", "paper_taker_fee_bps")
    @classmethod
    def _bounded_paper_bps(cls, value: str) -> str:
        """Paper slippage/fee are local sim constants; bound them (L4)."""
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("paper bps settings must be decimal strings") from exc
        if not parsed.is_finite() or parsed < 0 or parsed > 10000:
            raise ValueError("paper bps settings must be finite and within 0..10000 bps")
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

    @field_validator("archive_fixture_dir", mode="before")
    @classmethod
    def _empty_optional_path_is_unset(cls, value: object) -> object:
        """Treat a blank ``OFT_ARCHIVE_FIXTURE_DIR`` as unset, not ``Path('.')``."""
        if isinstance(value, (str, Path)) and not str(value).strip():
            return None
        return value

    @field_validator("archive_fixture_dir")
    @classmethod
    def _expand_optional_path(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator(*SECRET_FIELDS)
    @classmethod
    def _empty_secret_is_unset(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            return None
        return value


def validate_scan_period_setting(value: str) -> str:
    """Validate one ``OFT_SCAN_PERIOD`` value; return the stripped cadence.

    Shared by ``load_settings`` and the scheduler helpers so the supported-value
    rule and its message have one source of truth.
    """
    period = value.strip()
    if period not in SUPPORTED_SCAN_PERIODS:
        expected = ", ".join(SUPPORTED_SCAN_PERIODS)
        raise ValueError(f"{SCAN_PERIOD_ENV} must be one of: {expected}; got {value!r}")
    return period


def validate_scan_jitter_setting(value: int, period: str | None = None) -> int:
    """Validate one ``OFT_SCAN_JITTER_SECONDS`` value.

    A negative jitter is always invalid. When ``period`` is supplied the jitter
    must also be strictly shorter than that period, so a scheduler fire time
    can never land in a later window. Diagnostics keep the historical clamp
    (they pass no period); the scheduler helpers and the daemon-start
    overlay-cap check use this helper, while ``ScheduleState`` enforces the same
    bound independently.
    """
    if value < 0:
        raise ValueError(f"{SCAN_JITTER_ENV} must be >= 0")
    if period is not None:
        hours = SCAN_PERIOD_HOURS.get(period)
        if hours is None:
            raise ValueError(
                f"{SCAN_PERIOD_ENV} must be one of: {', '.join(SUPPORTED_SCAN_PERIODS)}"
            )
        limit = hours * 3600
        if value >= limit:
            raise ValueError(
                f"{SCAN_JITTER_ENV} must be less than the scan period ({limit} s for {period})"
            )
    return value


def validate_scan_schedule(period: str, jitter_seconds: int) -> str:
    """Validate the ``(period, jitter)`` pair; return the normalized period."""
    resolved_period = validate_scan_period_setting(period)
    validate_scan_jitter_setting(jitter_seconds, resolved_period)
    return resolved_period


class ConfigError(Exception):
    """Configuration failed validation; surfaced as ``CONFIG_INVALID``."""

    code = CONFIG_INVALID

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _format_validation_error(exc: ValidationError) -> str:
    """Render field paths and messages only.

    Pydantic's default ``str(exc)`` includes each error's ``input_value`` and
    documentation URL. An invalid base URL can therefore echo a credential that
    was embedded in it, so the input value never enters the CLI error path.
    """
    details: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        message = str(error.get("msg", "invalid value"))
        details.append(f"{location}: {message}")
    return "; ".join(details) or "invalid configuration"


def load_settings(*, validate_scan_cadence: bool = False, **overrides: Any) -> Settings:
    """Load settings from the environment, applying explicit overrides last.

    Invalid settings are wrapped so every entry point can report the stable
    ``CONFIG_INVALID`` code instead of leaking a traceback.

    ``OFT_SCAN_PERIOD`` / ``OFT_SCAN_JITTER_SECONDS`` are validated when the
    caller asks for it (``validate_scan_cadence=True``) or when an explicit
    override is supplied. Environment-only values are also validated by the
    paths that actually consume them (``daemon start`` and the scheduler
    helpers), so diagnostics can still resolve a persisted ``scan.period`` over
    a stale environment value (M3).
    """
    try:
        settings = Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    if validate_scan_cadence or "scan_period" in overrides or "scan_jitter_seconds" in overrides:
        try:
            validate_scan_schedule(settings.scan_period, settings.scan_jitter_seconds)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    return settings
