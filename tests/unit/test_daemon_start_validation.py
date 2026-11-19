"""``daemon start`` cadence validation (slice 58, R1).

The daemon must validate the *effective* (persisted-first) cadence, not the raw
``OFT_SCAN_PERIOD``. These tests exercise ``daemon_start`` itself with the HTTP
server and background workers patched out, so the command reaches the real
``_schedule_state`` overlay without ever blocking on uvicorn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import oft.cli.commands.daemon as daemon_module
import oft.cli.main as cli_main
from oft.adapters.sqlite_store import SqliteStore
from oft.application.settings_service import OVERLAY_MAX_JITTER_SECONDS
from oft.cli.main import app as cli_app
from oft.config import load_settings
from oft.domain.settings import SCAN_PERIOD_KEY
from oft.ports.store import ConfigRecord


class _UvicornReached(Exception):
    """Sentinel: validation passed and ``daemon_start`` reached the server."""


def _clear_oft_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("OFT_"):
            monkeypatch.delenv(key, raising=False)


def _seed_persisted_period(settings: Any, value: str) -> None:
    store = SqliteStore(settings)
    store.initialize()
    store.set_config(
        ConfigRecord(
            key=SCAN_PERIOD_KEY,
            value_json=json.dumps(value),
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )


def _patch_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every worker with a no-op and stop at the HTTP server."""
    noop = lambda *args, **kwargs: None  # noqa: E731 - tiny local stub

    def _reached_uvicorn(*args: Any, **kwargs: Any) -> None:
        raise _UvicornReached

    for name in (
        "_start_scheduler",
        "_start_settings_sync_scheduler",
        "_start_trigger_scheduler",
        "_start_position_monitor_scheduler",
        "_start_management_scheduler",
        "_start_telegram_runtime",
    ):
        monkeypatch.setattr(daemon_module, name, noop)
    monkeypatch.setattr(cli_main, "_run_live_ingestion", noop)
    monkeypatch.setattr(cli_main, "_run_uvicorn", _reached_uvicorn)


def test_daemon_start_lets_a_persisted_period_win_over_a_stale_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A saved 8h must not be rejected because ``OFT_SCAN_PERIOD`` is stale."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")
    _seed_persisted_period(load_settings(), "8h")
    _patch_background(monkeypatch)

    with pytest.raises(_UvicornReached):
        CliRunner().invoke(cli_app, ["daemon", "start"], catch_exceptions=False)


def test_daemon_start_rejects_an_invalid_env_period_without_a_persisted_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")
    _patch_background(monkeypatch)

    result = CliRunner().invoke(cli_app, ["daemon", "start"])

    assert result.exit_code == 5, result.output
    assert "OFT_SCAN_PERIOD" in result.output


def test_daemon_start_rejects_a_negative_jitter_even_with_a_persisted_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The jitter is never persisted, so a negative value always fails."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "-1")
    _seed_persisted_period(load_settings(), "8h")
    _patch_background(monkeypatch)

    result = CliRunner().invoke(cli_app, ["daemon", "start"])

    assert result.exit_code == 5, result.output
    assert "OFT_SCAN_JITTER_SECONDS" in result.output


def test_daemon_start_rejects_an_above_cap_jitter_even_with_a_persisted_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A persisted period must not silently clamp an out-of-range jitter."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "30000")
    _seed_persisted_period(load_settings(), "8h")
    _patch_background(monkeypatch)

    result = CliRunner().invoke(cli_app, ["daemon", "start"])

    assert result.exit_code == 5, result.output
    assert "OFT_SCAN_JITTER_SECONDS" in result.output
    assert str(OVERLAY_MAX_JITTER_SECONDS) in result.output


def test_daemon_start_rejects_a_jitter_just_above_the_overlay_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", str(OVERLAY_MAX_JITTER_SECONDS + 1))
    _patch_background(monkeypatch)

    result = CliRunner().invoke(cli_app, ["daemon", "start"])

    assert result.exit_code == 5, result.output
    assert "OFT_SCAN_JITTER_SECONDS" in result.output


def test_daemon_start_accepts_a_jitter_at_the_overlay_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap value is valid for every supported period and must reach the server."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", str(OVERLAY_MAX_JITTER_SECONDS))
    _seed_persisted_period(load_settings(), "8h")
    _patch_background(monkeypatch)

    with pytest.raises(_UvicornReached):
        CliRunner().invoke(cli_app, ["daemon", "start"], catch_exceptions=False)


def test_daemon_start_allows_a_blank_jitter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blank jitter means the Settings default on the runtime path too (N2)."""
    _clear_oft_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "")
    _patch_background(monkeypatch)

    with pytest.raises(_UvicornReached):
        CliRunner().invoke(cli_app, ["daemon", "start"], catch_exceptions=False)
