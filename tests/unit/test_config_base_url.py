"""LLM base URLs are validated against key-exfiltration shapes (M18)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oft.config import ConfigError, Settings, load_settings


@pytest.mark.parametrize(
    "value",
    [
        "http://api.openai.com",
        "https://user:secret@api.openai.com",
        "https://api.openai.com?proxy=evil",
        "https://api.openai.com#fragment",
        "https://",
        "",
        "api.openai.com",
    ],
)
def test_insecure_or_malformed_base_urls_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(env="paper", llm_openai_base_url=value)


def test_a_loopback_http_base_url_is_allowed() -> None:
    settings = Settings(env="paper", llm_openai_base_url="http://127.0.0.1:8080/")

    assert settings.llm_openai_base_url == "http://127.0.0.1:8080"


def test_http_is_allowed_in_the_test_environment() -> None:
    settings = Settings(env="test", llm_anthropic_base_url="http://api.anthropic.test")

    assert settings.llm_anthropic_base_url == "http://api.anthropic.test"


def test_an_invalid_base_url_is_config_invalid_at_load() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(env="paper", llm_deepseek_base_url="http://api.deepseek.com")

    assert caught.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize(
    ("field", "host"),
    [
        ("llm_openai_base_url", "api.openai.com"),
        ("llm_anthropic_base_url", "api.anthropic.com"),
        ("llm_deepseek_base_url", "api.deepseek.com"),
    ],
)
def test_each_providers_official_host_is_accepted(field: str, host: str) -> None:
    settings = Settings(env="paper", **{field: f"https://{host}"})

    assert getattr(settings, field) == f"https://{host}"


def test_a_custom_host_is_rejected_without_an_explicit_opt_in() -> None:
    with pytest.raises(ValidationError):
        Settings(env="paper", llm_openai_base_url="https://attacker.example.com")


def test_a_custom_host_is_allowed_with_the_explicit_opt_in() -> None:
    settings = Settings(
        env="paper",
        llm_openai_base_url="https://attacker.example.com",
        llm_allow_custom_base_url=True,
    )

    assert settings.llm_openai_base_url == "https://attacker.example.com"


def test_a_custom_host_is_allowed_in_the_test_environment() -> None:
    settings = Settings(env="test", llm_openai_base_url="https://attacker.example.com")

    assert settings.llm_openai_base_url == "https://attacker.example.com"


def test_a_custom_host_is_config_invalid_at_load() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(env="paper", llm_openai_base_url="https://attacker.example.com")

    assert caught.value.code == "CONFIG_INVALID"
