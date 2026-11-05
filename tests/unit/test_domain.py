"""Unit tests for domain and logging helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.domain.symbols import summarize_universe
from oft.domain.time import utc_timestamp
from oft.logging import REDACTED, redact_secrets


def test_fixture_universe_summary() -> None:
    summary = summarize_universe(FixtureMarketData().load_universe())

    assert summary.count == 3
    assert summary.tradifi_count == 1
    assert {item.symbol for item in summary.symbols} == {"BTCUSDT", "ETHUSDT", "AAPLUSDT"}


def test_utc_timestamp_contract_format() -> None:
    value = datetime(2026, 11, 6, 4, 12, 0, 123456, tzinfo=UTC)

    assert utc_timestamp(value) == "2026-11-06T04:12:00.123Z"


def test_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        utc_timestamp(datetime(2026, 11, 6, 4, 12, 0))


def test_redaction_masks_credentials() -> None:
    event = redact_secrets(
        None,
        "info",
        {"event": "startup", "api_key": "secret-value", "token": "abc", "safe": "visible"},
    )

    assert event["api_key"] == REDACTED
    assert event["token"] == REDACTED
    assert event["safe"] == "visible"
    assert event["event"] == "startup"
