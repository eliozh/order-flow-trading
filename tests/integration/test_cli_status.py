"""Walking-skeleton integration test: CLI -> domain."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from oft.bootstrap import get_status
from oft.cli.main import app
from oft.config import Settings


def _configure_cli(monkeypatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def test_cli_status_json_uses_shared_domain_path(monkeypatch, tmp_path: Path) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    expected = get_status(settings)

    result = CliRunner().invoke(app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["data"]["universe"] == expected.universe.model_dump(mode="json")
    assert payload["data"]["env"] == expected.env
    assert payload["meta"]["provenance"] == "CALC"


def test_cli_status_table_is_stable(monkeypatch, tmp_path: Path) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    output = result.stdout
    assert "Environment" in output
    assert "PAPER" not in output
    assert "test" in output
    assert "BTCUSDT" in output
    assert "TRADIFI_PERPETUAL" in output


def test_cli_and_domain_share_the_same_symbols(monkeypatch, tmp_path: Path) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    domain = get_status(settings)

    result = CliRunner().invoke(app, ["status", "--json"])

    cli = json.loads(result.stdout)["data"]
    assert [item["symbol"] for item in cli["universe"]["symbols"]] == [
        item.symbol for item in domain.universe.symbols
    ]
    assert cli["universe"]["count"] == domain.universe.count
