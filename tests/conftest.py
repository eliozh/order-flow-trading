"""Shared pytest fixtures."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from oft.api.app import create_app
from oft.clock import FixedClock
from oft.config import Settings

REPLAY_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
REPLAY_WINDOW_START = datetime(2025, 11, 5, 0, 0, 0, tzinfo=UTC)
REPLAY_WINDOW_END = datetime(2025, 11, 5, 4, 0, 0, tzinfo=UTC)
REPLAY_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        allow_fixture_scan_debug=True,
    )


@pytest.fixture()
def fixed_clock() -> FixedClock:
    """A deterministic clock for every test that needs time."""
    return FixedClock(REPLAY_NOW)


@pytest.fixture()
def replay_fixture() -> Path:
    """The committed, checksummed aggTrade sample used by replay tests."""
    return REPLAY_FIXTURE


@pytest.fixture()
def replay_window() -> tuple[datetime, datetime]:
    """The committed sample's half-open 4h window."""
    return REPLAY_WINDOW_START, REPLAY_WINDOW_END


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def _free_ipv6_port() -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.bind(("::1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def live_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Run a real loopback daemon so CLI commands exercise the API path."""
    port = _free_port()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    monkeypatch.setenv("OFT_ALLOW_FIXTURE_SCAN_DEBUG", "true")
    resolved = Settings(
        env="test",
        port=port,
        data_dir=data_dir,
        allow_fixture_scan_debug=True,
    )

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(resolved),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("daemon thread exited before startup")
        if time.monotonic() > deadline:
            raise RuntimeError("daemon did not start within 10s")
        time.sleep(0.02)

    try:
        yield resolved
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def live_ipv6_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Run a loopback daemon bound to ``::1`` for bracketed-URL coverage."""
    if not _ipv6_loopback_available():
        pytest.skip("IPv6 loopback is not available")
    port = _free_ipv6_port()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_HOST", "::1")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    resolved = Settings(env="test", host="::1", port=port, data_dir=data_dir)

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(resolved),
            host="::1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("IPv6 daemon thread exited before startup")
        if time.monotonic() > deadline:
            raise RuntimeError("IPv6 daemon did not start within 10s")
        time.sleep(0.02)

    try:
        yield resolved
    finally:
        server.should_exit = True
        thread.join(timeout=5)
