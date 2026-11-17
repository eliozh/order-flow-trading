"""Replay neutrality: persisted settings and watchlist stars change no output.

The paper-loop replay pins its own fixtures and attempt stores, so operator
state in the live data directory must not reach it. This test runs the real
``oft replay run --json`` path twice, mutating ``scan.period`` and the watchlist
stars in between, and requires the same decision, trigger, order, fill, and
position values plus the same identity digest.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.cli.main import app
from oft.clock import FixedClock
from oft.config import Settings

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
RUN_ID = "settings-watchlist-neutral"


def _invoke(data_dir: Path, recordings: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)
    return CliRunner().invoke(
        app,
        [
            "replay",
            "run",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(recordings),
            "--run-id",
            RUN_ID,
            "--json",
        ],
    )


def _envelope(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout[result.stdout.index("{") :])


def _mutate_operator_state(data_dir: Path) -> None:
    """Persist a non-default scan period and a starred symbol."""
    settings = Settings(env="test", port=8765, data_dir=data_dir)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    service = SettingsService(
        store=container.store,
        clock=container.clock,
        settings=container.settings,
    )
    updated = service.update_scan_period("8h", actor="test")
    assert updated.value == "8h"
    assert updated.source.value == "user"

    changed = container.symbol_registry.set_star_with_audit("BTCUSDT", True, actor="test")
    assert changed is not None
    assert changed.after.starred is True
    assert [contract.symbol for contract in container.symbol_registry.list_starred()] == ["BTCUSDT"]


def test_settings_and_watchlist_changes_do_not_alter_replay_output(
    tmp_path: Path, replay_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    data_dir = tmp_path / "data"
    first = _invoke(data_dir, replay_recordings, monkeypatch)
    assert first.exit_code == 0, first.output
    first_payload = _envelope(first)
    first_data = first_payload["data"]
    assert first_data["deterministic"] is True
    first_summary_path = Path(first_data["summary_path"])
    assert first_summary_path.is_file()
    first_summary = json.loads(first_summary_path.read_text(encoding="utf-8"))

    # Act: change live operator state, preserve the first replay run, and replay
    # again with the same run id and pinned inputs.
    _mutate_operator_state(data_dir)
    shutil.move(str(first_summary_path.parent), str(tmp_path / "first-replay-run"))
    second = _invoke(data_dir, replay_recordings, monkeypatch)

    # Assert
    assert second.exit_code == 0, second.output
    second_payload = _envelope(second)
    second_data = second_payload["data"]
    assert second_data["deterministic"] is True

    assert second_data["decision_id"] == first_data["decision_id"]
    assert second_data["trigger_id"] == first_data["trigger_id"]
    assert second_data["order"]["order_id"] == first_data["order"]["order_id"]
    assert second_data["fill"]["fill_id"] == first_data["fill"]["fill_id"]
    assert second_data["position"]["position_id"] == first_data["position"]["position_id"]
    # The whole deterministic data block is identical; only the envelope's
    # random request id differs.
    assert second_data == first_data

    second_summary = json.loads(Path(second_data["summary_path"]).read_text(encoding="utf-8"))
    assert second_summary["identity_digest"] == first_summary["identity_digest"]
    assert second_summary["outcome"] == first_summary["outcome"]
