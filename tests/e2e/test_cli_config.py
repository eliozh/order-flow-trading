"""E2E: the ``oft config show|get|set|validate`` CLI surface.

Every test runs with a temporary ``OFT_DATA`` and ``OFT_ENV=test`` so the real
CLI tree exercises the local SQLite store and the document settings service
without a daemon. The envelopes, exit codes, and audit writes mirror the
``/api/v1/settings/scan`` contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.cli.commands.doctor import exit_code_for
from oft.cli.main import app
from oft.config import Settings

SENTINEL_KEY = "scan.period"


@pytest.fixture(autouse=True)
def _clear_ambient_scan_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's ambient cadence must never shape a deterministic CLI test."""
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    return settings


def _store(settings: Settings) -> SqliteStore:
    store = SqliteStore(settings)
    store.initialize()
    return store


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


def test_show_json_emits_the_success_envelope_and_scan_settings_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "show", "--json")

    # Assert
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["ok"] is True
    assert payload["error"] is None
    data = payload["data"]
    assert data["key"] == SENTINEL_KEY
    assert data["value"] == "4h"
    assert data["source"] == "default"
    assert data["supported"] == ["4h", "8h"]
    assert data["timezone"] == "UTC"
    assert data["next_run"].endswith("Z")
    assert data["updated_at"] is None
    assert data["updated_by"] is None


def test_show_table_mode_exits_zero_and_names_the_effective_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "show")

    # Assert
    assert result.exit_code == 0, result.output
    assert SENTINEL_KEY in result.stdout
    assert "Source" in result.stdout
    assert "4h" in result.stdout


def test_get_json_returns_the_three_field_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "get", SENTINEL_KEY, "--json")

    # Assert
    assert result.exit_code == 0, result.output
    assert _json(result)["data"] == {
        "key": SENTINEL_KEY,
        "value": "4h",
        "source": "default",
    }


def test_unknown_key_emits_not_found_and_the_catalogued_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "get", "risk.per_trade", "--json")

    # Assert
    assert result.exit_code == exit_code_for("NOT_FOUND")
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "NOT_FOUND"


def test_set_persists_one_row_and_one_audit_then_a_repeat_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    settings = _configure(monkeypatch, tmp_path)
    runner = CliRunner()

    # Act
    first = runner.invoke(app, ["config", "set", SENTINEL_KEY, "8h", "--json"])
    store = _store(settings)
    rows_after_first = store.list_config()
    audits_after_first = store.list_audit_events()

    repeated = runner.invoke(app, ["config", "set", SENTINEL_KEY, "8h", "--json"])
    rows_after_repeat = store.list_config()
    audits_after_repeat = store.list_audit_events()

    # Assert
    assert first.exit_code == 0, first.output
    first_data = _json(first)["data"]
    assert first_data["value"] == "8h"
    assert first_data["source"] == "user"
    assert first_data["updated_by"] == "cli"
    assert len(rows_after_first) == 1
    assert rows_after_first[0].key == SENTINEL_KEY
    assert json.loads(rows_after_first[0].value_json) == "8h"
    assert len(audits_after_first) == 1
    assert audits_after_first[0].event_type == "CONFIG_UPDATED"

    assert repeated.exit_code == 0, repeated.output
    assert _json(repeated)["data"]["value"] == "8h"
    assert len(rows_after_repeat) == 1
    assert audits_after_repeat == audits_after_first


def test_set_invalid_value_exits_validation_failed_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    settings = _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "set", SENTINEL_KEY, "3h", "--json")
    store = _store(settings)

    # Assert
    assert result.exit_code == exit_code_for("VALIDATION_FAILED")
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION_FAILED"
    assert payload["error"]["details"][0]["field"] == "period"
    assert store.list_config() == ()
    assert store.list_audit_events() == ()


def test_validate_exits_zero_and_writes_no_config_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    settings = _configure(monkeypatch, tmp_path)

    # Act
    result = _invoke("config", "validate", "--json")
    store = _store(settings)

    # Assert
    assert result.exit_code == 0, result.output
    data = _json(result)["data"]
    assert data["status"] == "PASS"
    assert data["key"] == SENTINEL_KEY
    assert data["value"] == "4h"
    assert store.list_config() == ()


def test_config_load_failure_maps_to_config_invalid_exit_five(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_PORT", "not-a-port")

    # Act
    result = _invoke("config", "show", "--json")

    # Assert
    assert result.exit_code == exit_code_for("CONFIG_INVALID")
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"
