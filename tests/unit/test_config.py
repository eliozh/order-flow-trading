"""Unit tests for typed settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from oft.config import ConfigError, Environment, Settings, load_settings


def _clear_oft_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("OFT_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.env is Environment.PAPER
    assert settings.host == "127.0.0.1"
    assert settings.port == 7611
    assert settings.data_dir == Path("data")
    assert settings.log_level == "INFO"
    assert settings.api_token is None
    assert settings.binance_api_key is None
    assert settings.openai_api_key is None


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_ENV", "live")
    monkeypatch.setenv("OFT_HOST", "127.0.0.1")
    monkeypatch.setenv("OFT_PORT", "9000")
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "market-data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "debug")

    settings = Settings()

    assert settings.env is Environment.LIVE
    assert settings.port == 9000
    assert settings.data_dir == tmp_path / "market-data"
    assert settings.log_level == "DEBUG"


def test_oft_data_alias_is_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "aliased-data"))

    settings = load_settings()

    assert settings.data_dir == tmp_path / "aliased-data"


def test_explicit_overrides_win_over_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.setenv("OFT_PORT", "9999")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "from-env"))

    settings = load_settings(port=7611, data_dir=tmp_path / "explicit")

    assert settings.port == 7611
    assert settings.data_dir == tmp_path / "explicit"


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(port=port)


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD")


def test_invalid_config_is_reported_as_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.setenv("OFT_PORT", "0")

    with pytest.raises(ConfigError) as caught:
        load_settings()

    assert caught.value.code == "CONFIG_INVALID"
    assert "port" in caught.value.message.lower()


def test_invalid_log_level_is_reported_as_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.setenv("OFT_LOG_LEVEL", "LOUD")

    with pytest.raises(ConfigError) as caught:
        load_settings()

    assert caught.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize(
    "variable", ["OFT_LLM_MAX_CONCURRENCY", "OFT_LLM_MAX_CONCURRENCY_DEEPSEEK"]
)
def test_out_of_range_llm_concurrency_is_rejected_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    """H1: the environment bound matches MAX_CONCURRENCY; >64 is a ConfigError."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, "65")

    with pytest.raises(ConfigError) as caught:
        load_settings()

    assert caught.value.code == "CONFIG_INVALID"
    assert "llm_max_concurrency" in caught.value.message.lower()


def test_a_blank_provider_is_unset_and_an_unknown_one_is_config_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H2: an unknown OFT_LLM_PROVIDER is typed, never a composition traceback."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_LLM_PROVIDER", "   ")
    assert load_settings().llm_provider is None

    monkeypatch.setenv("OFT_LLM_PROVIDER", "bogus")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.code == "CONFIG_INVALID"
    assert "llm_provider" in caught.value.message.lower()


def test_secrets_are_not_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.api_token is None
    assert settings.telegram_bot_token is None


def test_empty_secret_placeholders_are_unset() -> None:
    settings = Settings(api_token="", openai_api_key="   ")

    assert settings.api_token is None
    assert settings.openai_api_key is None


def test_scan_cadence_defaults_are_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.scan_period == "4h"
    assert settings.scan_jitter_seconds == 30


def test_valid_scan_cadence_loads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", "8h")
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "10")

    settings = load_settings()

    assert settings.scan_period == "8h"
    assert settings.scan_jitter_seconds == 10


def test_valid_explicit_scan_cadence_loads() -> None:
    settings = load_settings(scan_period="8h", scan_jitter_seconds=0)

    assert settings.scan_period == "8h"
    assert settings.scan_jitter_seconds == 0


@pytest.mark.parametrize("period", ["1h", "", "4H"])
def test_invalid_scan_period_is_config_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, period: str
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", period)

    with pytest.raises(ConfigError) as caught:
        load_settings(validate_scan_cadence=True)

    assert caught.value.code == "CONFIG_INVALID"
    assert "OFT_SCAN_PERIOD" in caught.value.message


def test_invalid_explicit_scan_period_is_config_invalid() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(scan_period="1h")

    assert caught.value.code == "CONFIG_INVALID"
    assert "OFT_SCAN_PERIOD" in caught.value.message


def test_negative_scan_jitter_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "-1")

    with pytest.raises(ConfigError) as caught:
        load_settings(validate_scan_cadence=True)

    assert caught.value.code == "CONFIG_INVALID"
    assert "OFT_SCAN_JITTER_SECONDS" in caught.value.message


def test_scan_jitter_must_be_shorter_than_the_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", "4h")
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", str(4 * 3600))

    with pytest.raises(ConfigError) as caught:
        load_settings(validate_scan_cadence=True)

    assert caught.value.code == "CONFIG_INVALID"
    assert "less than the scan period" in caught.value.message


def test_empty_archive_fixture_dir_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty OFT_ARCHIVE_FIXTURE_DIR disables fixtures, not ``Path('.')``."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", "")

    assert load_settings().archive_fixture_dir is None
    assert Settings(archive_fixture_dir="   ").archive_fixture_dir is None


def test_plain_load_defers_scan_period_validation_for_m3_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Diagnostics must be able to overlay a persisted period over a stale env.

    ``daemon status`` / ``scan status`` / ``config show`` resolve the persisted
    row first (M3), so a plain load keeps the raw environment value available
    for that overlay. The strict path (``daemon start`` and the scheduler
    helpers) rejects it.
    """
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")

    settings = load_settings()

    assert settings.scan_period == "1h"
    with pytest.raises(ConfigError):
        load_settings(validate_scan_cadence=True)


def test_strict_load_rejects_a_non_integer_scan_jitter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "soon")

    with pytest.raises(ConfigError) as caught:
        load_settings(validate_scan_cadence=True)

    assert caught.value.code == "CONFIG_INVALID"


def test_padded_scan_period_is_stripped_and_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", " 4h ")

    settings = load_settings(validate_scan_cadence=True)

    assert settings.scan_period == "4h"


def test_padded_unsupported_scan_period_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", " 1h ")

    with pytest.raises(ConfigError) as caught:
        load_settings(validate_scan_cadence=True)

    assert caught.value.code == "CONFIG_INVALID"
    assert "OFT_SCAN_PERIOD" in caught.value.message


@pytest.mark.parametrize("jitter", ["", "   "])
def test_blank_scan_jitter_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, jitter: str
) -> None:
    """A blank ``OFT_SCAN_JITTER_SECONDS`` must not break plain diagnostics."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", jitter)

    settings = load_settings()

    assert settings.scan_jitter_seconds == 30
    assert Settings(scan_jitter_seconds=jitter).scan_jitter_seconds == 30


def test_invalid_explicit_scan_cadence_pair_is_config_invalid() -> None:
    """The (period, jitter) pair is validated even with explicit overrides."""
    with pytest.raises(ConfigError) as caught:
        load_settings(scan_period="4h", scan_jitter_seconds=4 * 3600)

    assert caught.value.code == "CONFIG_INVALID"
    assert "less than the scan period" in caught.value.message
