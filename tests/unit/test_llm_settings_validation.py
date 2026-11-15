"""Pure LLM settings validation: base URLs, roles, and concurrency (slice 35)."""

from __future__ import annotations

import pytest

from oft.domain.llm_settings import (
    ROLE_NAMES,
    LLMSettingsValidationError,
    validate_api_key,
    validate_base_url,
    validate_concurrency,
    validate_provider,
    validate_roles,
)


def _detail(exc: LLMSettingsValidationError) -> dict[str, object]:
    assert exc.details, exc.message
    return exc.details[0]


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("openai", "https://api.openai.com"),
        ("openai", "https://api.openai.com/v1"),
        ("anthropic", "https://api.anthropic.com"),
        ("deepseek", "https://api.deepseek.com/"),
        ("openai", "http://127.0.0.1:8080"),
        ("anthropic", "http://localhost:9000"),
    ],
)
def test_official_and_loopback_base_urls_are_accepted(provider: str, url: str) -> None:
    assert validate_base_url(url, provider=provider, allow_custom=False)


def test_http_non_loopback_is_rejected_and_names_the_rule() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url("http://api.openai.com", provider="openai", allow_custom=False)
    detail = _detail(excinfo.value)
    assert detail["field"] == "base_urls.openai"
    assert detail["code"] == "https_required"


def test_http_is_allowed_only_for_loopback() -> None:
    assert validate_base_url("http://127.0.0.1:8080", provider="openai", allow_custom=True)
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url("http://gateway.internal:8080", provider="openai", allow_custom=True)
    assert _detail(excinfo.value)["code"] == "https_required"


def test_userinfo_is_rejected() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url("https://user:pass@api.openai.com", provider="openai", allow_custom=False)
    assert _detail(excinfo.value)["code"] == "userinfo_forbidden"


def test_query_is_rejected() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url(
            "https://api.openai.com?redirect=evil",
            provider="openai",
            allow_custom=False,
        )
    detail = _detail(excinfo.value)
    assert detail["field"] == "base_urls.openai"
    assert detail["code"] == "query_forbidden"


def test_fragment_is_rejected() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url("https://api.openai.com#frag", provider="openai", allow_custom=False)
    assert _detail(excinfo.value)["code"] == "fragment_forbidden"


def test_unofficial_host_is_rejected_unless_the_operator_opts_in() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url(
            "https://api.anthropic.com.evil.example",
            provider="anthropic",
            allow_custom=False,
        )
    detail = _detail(excinfo.value)
    assert detail["field"] == "base_urls.anthropic"
    assert detail["code"] == "unofficial_host"

    assert (
        validate_base_url(
            "https://api.anthropic.com.evil.example",
            provider="anthropic",
            allow_custom=True,
        )
        == "https://api.anthropic.com.evil.example"
    )


def test_unknown_provider_and_role_are_rejected() -> None:
    with pytest.raises(LLMSettingsValidationError):
        validate_provider("stub")
    assert validate_provider(" OpenAI ") == "openai"
    assert validate_provider(None) is None

    with pytest.raises(LLMSettingsValidationError):
        validate_roles({"TEMPERATURE": "1"})


def test_roles_are_normalized_without_inventing_extra_fields() -> None:
    roles = validate_roles({"ANALYSIS": " claude-sonnet ", "REVIEW": ""})
    assert roles == {
        "ANALYSIS": "claude-sonnet",
        "ENTRY_RECHECK": None,
        "MANAGEMENT": None,
        "REVIEW": None,
    }
    assert tuple(roles) == ROLE_NAMES


@pytest.mark.parametrize("value", [0, -1, 65, True, "3", 1.5])
def test_invalid_concurrency_is_rejected(value: object) -> None:
    with pytest.raises(LLMSettingsValidationError):
        validate_concurrency(value)


@pytest.mark.parametrize("value", [1, 2, 64])
def test_valid_concurrency_is_returned(value: int) -> None:
    assert validate_concurrency(value) == value


def test_api_key_rejects_a_non_string_and_internal_whitespace() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_api_key(1234)  # type: ignore[arg-type]
    assert _detail(excinfo.value)["code"] == "not_a_string"
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_api_key("sk-bad key-1234-4f2a")
    assert _detail(excinfo.value)["code"] == "invalid_characters"


def test_provider_rejects_a_non_string_and_treats_blank_as_unset() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_provider(7)  # type: ignore[arg-type]
    assert _detail(excinfo.value)["code"] == "invalid_type"
    assert validate_provider("   ") is None


def test_roles_handle_none_non_mapping_non_string_and_spaces() -> None:
    assert validate_roles(None) == {
        "ANALYSIS": None,
        "ENTRY_RECHECK": None,
        "MANAGEMENT": None,
        "REVIEW": None,
    }
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_roles(["ANALYSIS"])  # type: ignore[arg-type]
    assert _detail(excinfo.value)["code"] == "not_an_object"
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_roles({"ANALYSIS": 3})
    assert _detail(excinfo.value)["code"] == "invalid_type"
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_roles({"ANALYSIS": "bad model"})
    assert _detail(excinfo.value)["code"] == "invalid_model"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (42, "invalid_type"),
        ("   ", "empty"),
        ("https://", "missing_host"),
        ("https://api.openai.com:notaport", "invalid_url"),
    ],
)
def test_base_url_rejects_malformed_values(value: object, code: str) -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_base_url(value, provider="openai", allow_custom=False)
    assert _detail(excinfo.value)["code"] == code
