"""Pure Telegram message renderers (no emoji, deterministic, bounded)."""

from __future__ import annotations

from datetime import UTC, datetime

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.application.status import build_status
from oft.application.telegram_messages import (
    render_help,
    render_status,
    render_test_message,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import HELP_COMMAND, STATUS_COMMAND

NOW = datetime(2026, 11, 17, 9, 30, 0, tzinfo=UTC)


def _settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def test_help_lists_only_the_two_supported_commands() -> None:
    text = render_help()

    assert STATUS_COMMAND in text
    assert HELP_COMMAND in text
    commands = {token for token in text.split() if token.startswith("/")}
    assert commands == {STATUS_COMMAND, HELP_COMMAND}
    assert text.isascii()


def test_status_reply_numbers_equal_build_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    market_data = FixtureMarketData(fixtures_dir=settings.fixtures_dir)
    clock = FixedClock(NOW)
    report = build_status(settings, market_data, clock, store=None)

    text = render_status(report)
    lines = text.splitlines()

    assert lines[0] == "STATUS"
    assert f"env: {report.env}" in lines
    assert f"version: {report.version}" in lines
    assert f"daemon: {report.daemon.state}" in lines
    assert f"symbols: {report.universe.count}" in lines
    assert f"tradifi_symbols: {report.universe.tradifi_count}" in lines
    assert f"generated_at: {report.generated_at}" in lines
    assert report.generated_at == "2026-11-17T09:30:00.000Z"
    assert text.isascii()


def test_status_renders_the_last_scan_numbers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from oft.application.status import (
        DaemonStatus,
        LastScanSummary,
        StatusReport,
    )
    from oft.domain.symbols import SymbolUniverseSummary

    report = StatusReport(
        env="paper",
        version="0.1.0",
        host="127.0.0.1",
        port=7611,
        data_dir="/tmp/data",
        log_level="INFO",
        generated_at="2026-11-17T09:30:00.000Z",
        universe=SymbolUniverseSummary(
            source="fixture",
            as_of="2026-11-17T00:00:00.000Z",
            count=2,
            tradifi_count=1,
            symbols=(),
        ),
        daemon=DaemonStatus(state="UP", detail="served"),
        last_scan=LastScanSummary(
            scan_id="SCAN-1",
            symbol="BTCUSDT",
            period="4h",
            status="COMPLETE",
            delta="12.5",
            cvd="42.0",
            finished_at="2026-11-17T08:00:00.000Z",
        ),
    )

    lines = render_status(report).splitlines()

    assert "last_scan: SCAN-1" in lines
    assert "last_scan_symbol: BTCUSDT" in lines
    assert "last_scan_delta: 12.5" in lines
    assert "last_scan_cvd: 42.0" in lines


def test_test_message_is_deterministic_and_ascii() -> None:
    first = render_test_message("2026-11-17T09:30:00.000Z")
    second = render_test_message("2026-11-17T09:30:00.000Z")

    assert first == second
    assert first.splitlines()[0] == "TELEGRAM TEST"
    assert "2026-11-17T09:30:00.000Z" in first
    assert first.isascii()
