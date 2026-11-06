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
