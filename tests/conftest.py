"""Shared pytest fixtures."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from oft.api.app import create_app
from oft.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
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
    resolved = Settings(env="test", port=port, data_dir=data_dir)

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
