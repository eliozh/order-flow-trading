"""Unit tests for the ``scan.period`` domain contract (stdlib only)."""

from __future__ import annotations

import pytest

from oft.domain.settings import (
    SCAN_PERIOD_KEY,
    SUPPORTED_SCAN_PERIODS,
    SettingSource,
    SettingValidationError,
    scoped_diff,
    validate_scan_period,
)


def test_only_scan_period_and_the_two_supported_values_exist() -> None:
    assert SCAN_PERIOD_KEY == "scan.period"
    assert SUPPORTED_SCAN_PERIODS == ("4h", "8h")


def test_source_enum_has_exactly_default_and_user() -> None:
    assert {source.value for source in SettingSource} == {"default", "user"}
    assert SettingSource.DEFAULT.value == "default"
    assert SettingSource.USER.value == "user"


@pytest.mark.parametrize("value", ["4h", "8h", " 8h ", "4h\n"])
def test_supported_periods_are_normalized_and_returned(value: str) -> None:
    assert validate_scan_period(value) in SUPPORTED_SCAN_PERIODS


@pytest.mark.parametrize(
    ("value", "detail_code"),
    [
        ("1h", "unsupported"),
        ("2h", "unsupported"),
        ("3h", "unsupported"),
        ("12h", "unsupported"),
        ("24h", "unsupported"),
        ("eight", "unsupported"),
        ("8H", "unsupported"),
        ("", "empty"),
        ("   ", "empty"),
        (8, "not_a_string"),
        (True, "not_a_string"),
        (None, "not_a_string"),
        (["8h"], "not_a_string"),
    ],
)
def test_invalid_values_raise_a_field_detail(value: object, detail_code: str) -> None:
    with pytest.raises(SettingValidationError) as info:
        validate_scan_period(value)

    error = info.value
    assert error.code == "VALIDATION_FAILED"
    assert error.message
    assert len(error.details) == 1
    assert error.details[0]["field"] == "period"
    assert error.details[0]["code"] == detail_code
    assert error.details[0]["message"]


def test_scoped_diff_carries_only_the_key_and_before_after() -> None:
    diff = scoped_diff(key=SCAN_PERIOD_KEY, before="4h", after="8h")

    assert diff == {"key": "scan.period", "before": "4h", "after": "8h"}
