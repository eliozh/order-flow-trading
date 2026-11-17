"""E2E: ``oft watchlist add|remove`` and the ``star|unstar`` aliases.

A real loopback daemon serves the local API so these tests exercise the same
mutating path the Web client uses. The registry is seeded from the committed
exchangeInfo fixture by the daemon bootstrap; the CLI reads the SQLite rows
back for ``list --starred``.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.cli.commands.doctor import exit_code_for
from oft.cli.main import app
from oft.config import Settings

SENTINEL_SYMBOL = "BTCUSDT"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


def _listed_symbols(runner: CliRunner) -> list[str]:
    listed = runner.invoke(app, ["watchlist", "list", "--starred", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = _json(listed)
    assert payload["data"]["starred"] is True
    return [item["symbol"] for item in payload["data"]["items"]]


def test_add_starred_is_observable_and_remove_reverses_it(live_daemon: Settings) -> None:
    # Arrange
    runner = CliRunner()

    # Act
    added = runner.invoke(app, ["watchlist", "add", SENTINEL_SYMBOL, "--json"])
    observed = _listed_symbols(runner)
    removed = runner.invoke(app, ["watchlist", "remove", SENTINEL_SYMBOL, "--json"])
    after_removal = _listed_symbols(runner)

    # Assert
    assert added.exit_code == 0, added.output
    added_payload = _json(added)
    assert added_payload["ok"] is True
    assert added_payload["data"]["symbol"] == SENTINEL_SYMBOL
    assert added_payload["data"]["starred"] is True
    assert added_payload["data"]["starred_at"]
    assert SENTINEL_SYMBOL in observed

    assert removed.exit_code == 0, removed.output
    assert _json(removed)["data"] == {
        "symbol": SENTINEL_SYMBOL,
        "starred": False,
        "starred_at": None,
    }
    assert SENTINEL_SYMBOL not in after_removal


def test_add_matches_star_and_remove_matches_unstar(live_daemon: Settings) -> None:
    # Arrange
    runner = CliRunner()

    # Act
    added = runner.invoke(app, ["watchlist", "add", SENTINEL_SYMBOL, "--json"])
    removed = runner.invoke(app, ["watchlist", "remove", SENTINEL_SYMBOL, "--json"])
    starred = runner.invoke(app, ["watchlist", "star", SENTINEL_SYMBOL, "--json"])
    unstarred = runner.invoke(app, ["watchlist", "unstar", SENTINEL_SYMBOL, "--json"])

    # Assert
    for result in (added, removed, starred, unstarred):
        assert result.exit_code == 0, result.output

    # ``starred_at`` is the server's clock, so compare every other field.
    def _stable(result: Any) -> dict[str, Any]:
        data = dict(_json(result)["data"])
        data.pop("starred_at", None)
        return data

    assert _stable(added) == _stable(starred)
    assert _stable(removed) == _stable(unstarred)
    assert _json(added)["data"]["starred"] is True
    assert _json(starred)["data"]["starred"] is True
    assert _json(removed)["data"]["starred"] is False
    assert _json(unstarred)["data"]["starred"] is False


def test_unknown_symbol_maps_to_the_catalogued_not_found_exit(
    live_daemon: Settings,
) -> None:
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(app, ["watchlist", "add", "NOPEUSDT", "--json"])

    # Assert
    assert result.exit_code == exit_code_for("NOT_FOUND")
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


def test_daemon_offline_maps_to_daemon_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange: a free port with no listener and an empty data dir.
    port = _free_port()
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    runner = CliRunner()

    # Act
    result = runner.invoke(app, ["watchlist", "add", SENTINEL_SYMBOL, "--json"])

    # Assert
    assert result.exit_code == exit_code_for("DAEMON_NOT_RUNNING")
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DAEMON_NOT_RUNNING"
