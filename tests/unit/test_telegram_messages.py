"""Pure Telegram message renderers (no emoji, deterministic, bounded)."""

from __future__ import annotations

from datetime import UTC, datetime

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.application.status import build_status
from oft.application.telegram_messages import (
    STATUS_HELP_FIELDS,
    render_help,
    render_status,
    render_test_message,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import COMMANDS, STATUS_COMMAND

NOW = datetime(2026, 11, 17, 9, 30, 0, tzinfo=UTC)


def _settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def test_help_lists_the_closed_command_table_and_the_read_only_notice() -> None:
    text = render_help()

    commands = {token for token in text.split() if token.startswith("/")}
    assert commands == set(COMMANDS)
    assert "MVP 只读" in text
    assert "/approve" not in text


def test_help_status_description_only_advertises_fields_render_status_produces() -> None:
    from oft.application.status import (
        DaemonStatus,
        LastScanSummary,
        ScheduleStatus,
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
        schedule=ScheduleStatus(
            period="4h",
            next_run_at="2026-11-17T12:00:00.000Z",
        ),
    )
    rendered_keys = {
        line.split(":", 1)[0] for line in render_status(report).splitlines() if ":" in line
    }
    help_status_line = next(
        line for line in render_help().splitlines() if line.startswith(f"{STATUS_COMMAND} - ")
    )

    for key, label in STATUS_HELP_FIELDS:
        assert key in rendered_keys, f"help advertises {key!r} but render_status omits it"
        assert label in help_status_line
    # The two fields deferred out of this slice are not advertised.
    assert "持仓数" not in help_status_line
    assert "预算" not in help_status_line


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
