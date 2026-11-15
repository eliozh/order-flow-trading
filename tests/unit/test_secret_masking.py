"""Write-only secret masking and fingerprinting (slice 35).

The mask may expose only the last four characters, always behind a fixed
prefix; a key's prefix, length, and full value must never be derivable from any
returned artifact.
"""

from __future__ import annotations

import hashlib

import pytest

from oft.domain.llm_settings import (
    LLMSettingsValidationError,
    fingerprint_secret,
    last4,
    mask_secret,
    validate_api_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-ant-api03-abcdef4f2a", "****4f2a"),
        ("abcdefghijklmnop", "****mnop"),
        ("abcdefgh", "****"),
        ("1234", "****"),
        ("abc", "****"),
        ("", "****"),
    ],
)
def test_mask_shows_only_the_last_four_characters(value: str, expected: str) -> None:
    assert mask_secret(value) == expected


def test_mask_never_contains_the_key_prefix_or_full_value() -> None:
    key = "sk-live-SENTINEL-9b71"
    masked = mask_secret(key)
    assert masked == "****9b71"
    assert key not in masked
    assert key[:8] not in masked
    assert len(masked) == 8


def test_mask_output_is_stable_and_last4_is_a_suffix() -> None:
    key = "sk-test-1234-abcdef"
    assert mask_secret(key) == mask_secret(key)
    assert mask_secret(key).endswith(last4(key))


def test_a_short_value_can_never_be_fully_revealed_by_the_mask() -> None:
    for value in ("a", "abcd", "abcdefghijklmno"):
        assert mask_secret(value) == "****"
        assert last4(value) == ""


def test_fingerprint_is_sha256_and_never_the_value() -> None:
    key = "sk-fingerprint-source"
    digest = fingerprint_secret(key)
    assert digest == hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert key not in digest
    assert digest != fingerprint_secret(f"{key}-rotated")


@pytest.mark.parametrize("value", ["", "   ", "\n\t", "abc", "sk-short-1234"])
def test_api_key_validation_rejects_blank_or_too_short_values(value: str) -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_api_key(value)
    assert excinfo.value.details[0]["field"] == "api_key"


def test_api_key_validation_rejects_an_unbounded_length() -> None:
    with pytest.raises(LLMSettingsValidationError) as excinfo:
        validate_api_key("sk-" + "a" * 600)
    assert excinfo.value.details[0]["code"] == "too_long"


@pytest.mark.parametrize("value", ["sk-valid-key-4f2a", "  sk-trimmed-key-1234  "])
def test_api_key_validation_strips_and_accepts_a_key(value: str) -> None:
    normalized = validate_api_key(value)
    assert normalized == value.strip()
    assert "\n" not in normalized
    assert "\t" not in normalized


def test_last4_is_empty_below_the_minimum_length() -> None:
    assert last4("abc") == ""
    assert last4("abcd") == ""
    assert last4("abcdefghijklmnop") == "mnop"
