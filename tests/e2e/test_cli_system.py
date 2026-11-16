"""E2E: the real CLI tree for ``system info|logs`` and ``daemon restart``.

A real loopback ``uvicorn`` daemon is started on a free port with the ``OFT_*``
environment pointing at a temporary data directory. Diagnostics run both before
the PID file exists (offline) and with one (running). The restart journey uses a
real short-lived child process as the old daemon so the SIGTERM/reap path runs,
while the spawn/wait seams are replaced so no second daemon is ever launched.
Every process/server started here is stopped and reaped in ``finally``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.commands import daemon as daemon_module
from oft.cli.commands.daemon import log_path, pid_meta_path, pid_path, write_pid
from oft.cli.main import app as cli_app
from oft.config import Settings

_SECRET_ENV = (
    "OFT_OPENAI_API_KEY",
    "OFT_ANTHROPIC_API_KEY",
    "OFT_DEEPSEEK_API_KEY",
    "OFT_BINANCE_API_KEY",
    "OFT_BINANCE_API_SECRET",
    "OFT_API_TOKEN",
    "OFT_TELEGRAM_BOT_TOKEN",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _daemon_settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=_free_port(), data_dir=tmp_path / "data")


def _cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    for name in _SECRET_ENV:
        monkeypatch.delenv(name, raising=False)


@contextmanager
def _running_daemon(settings: Settings) -> Iterator[None]:
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=settings.port,
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
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "daemon thread did not stop after join"


@pytest.fixture()
def daemon_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.chdir(tmp_path)
    settings = _daemon_settings(tmp_path)
    _cli_env(monkeypatch, settings)
    yield settings


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli_app, list(args))


def test_system_info_offline_then_running_with_a_real_daemon(
    daemon_layout: Settings,
) -> None:
    offline = _invoke("system", "info", "--json")
    assert offline.exit_code == 0, offline.output
    assert json.loads(offline.stdout)["data"]["daemon"]["state"] == "OFFLINE"

    with _running_daemon(daemon_layout):
        write_pid(daemon_layout)
        try:
            running = _invoke("system", "info", "--json")
            assert running.exit_code == 0, running.output
            daemon = json.loads(running.stdout)["data"]["daemon"]
            assert daemon["state"] == "RUNNING"
            assert daemon["pid"] == os.getpid()
            assert daemon["uptime_seconds"] is not None
            assert daemon["uptime_seconds"] >= 0
        finally:
            pid_path(daemon_layout).unlink(missing_ok=True)
            pid_meta_path(daemon_layout).unlink(missing_ok=True)


def test_system_logs_filters_the_daemon_data_dir_log(daemon_layout: Settings) -> None:
    path = log_path(daemon_layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": "2026-10-08T12:00:00.000Z", "level": "info", "event": "old_event"},
        {"timestamp": "2026-10-08T12:05:00.000Z", "level": "warning", "event": "warn_event"},
        {"timestamp": "2026-10-08T12:10:00.000Z", "level": "error", "event": "error_event"},
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    with _running_daemon(daemon_layout):
        result = _invoke(
            "system",
            "logs",
            "--since",
            "2026-10-08T12:04:00.000Z",
            "--level",
            "warning",
            "--json",
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["path"] == str(path)
    assert [line["event"] for line in data["lines"]] == ["warn_event", "error_event"]


def test_daemon_restart_journey_stops_a_real_child_and_never_closes_positions(
    daemon_layout: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real short-lived child process stands in for the old daemon.
    old_daemon = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path(daemon_layout).parent.mkdir(parents=True, exist_ok=True)
    pid_path(daemon_layout).write_text(str(old_daemon.pid), encoding="utf-8")
    # The PID fence fails closed, so it needs the recorded start time.
    pid_meta_path(daemon_layout).write_text(
        json.dumps(
            {"pid": old_daemon.pid, "start_time": daemon_module._process_start_time(old_daemon.pid)}
        ),
        encoding="utf-8",
    )

    container = build_container(daemon_layout, initialize=True)
    import sqlite3

    with sqlite3.connect(container.store.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, environment, currency, initial_balance, balance, updated_at) "
            "VALUES ('ACC-E2E', 'PAPER', 'USDT', '10000.00', '10000.00', "
            "'2026-10-08T12:00:00.000Z')"
        )
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES ('POS-E2E-RESTART', 'ACC-E2E', 'PAPER', 'BTCUSDT', 'OPEN', "
            "'2026-10-08T12:00:00.000Z', '{}')"
        )
        connection.commit()

    spawned: list[subprocess.Popen[bytes]] = []

    def fake_spawn(_settings: Settings) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        spawned.append(child)
        return child

    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", fake_spawn)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 999999)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)

    try:
        result = _invoke("daemon", "restart", "--yes", "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["data"] == {
            "state": "RESTARTED",
            "previous_pid": old_daemon.pid,
            "pid": 999999,
            "open_positions": 1,
        }
        # The warning stays on stderr so stdout is a clean envelope.
        assert "Open positions" in result.stderr
        assert "1 open position remains open" in result.stderr

        # The real stop helper SIGTERMed and reaped the child.
        assert old_daemon.wait(timeout=5) is not None
        assert old_daemon.poll() is not None

        positions = build_container(daemon_layout).store.list_positions(state="OPEN")
        assert [position.position_id for position in positions] == ["POS-E2E-RESTART"]
    finally:
        if old_daemon.poll() is None:
            old_daemon.terminate()
            try:
                old_daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old_daemon.kill()
                old_daemon.wait(timeout=5)
        for child in spawned:
            if child.poll() is None:
                child.wait(timeout=5)
        pid_path(daemon_layout).unlink(missing_ok=True)
        pid_meta_path(daemon_layout).unlink(missing_ok=True)
