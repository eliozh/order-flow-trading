"""The ``oft replay`` CLI: recorded success, JSON envelope, and human output."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.api.errors import ERROR_CATALOG
from oft.replay.paper_loop import (
    PaperFillSummary,
    PaperLoopResult,
    PaperOrderSummary,
    PaperPositionSummary,
)

pytestmark = pytest.mark.replay

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"


def test_the_cli_replays_recorded_output_as_json(
    tmp_path: Path, replay_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        [
            "replay",
            "run",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(replay_recordings),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    # structlog may emit an info line before the envelope; take the JSON body.
    envelope = json.loads(result.stdout[result.stdout.index("{") :])
    data = envelope["data"]
    assert data["deterministic"] is True
    assert data["decision_id"]
    assert data["order"]["order_id"]
    assert data["fill"]["fill_id"]
    assert data["fill"]["price"]
    assert data["position"]["position_id"]
    assert Path(data["summary_path"]).is_file()
    # The envelope meta mirrors the trigger snapshot's quality, not an empty
    # top-level trigger field.
    assert data["data_quality"] == "PARTIAL"
    assert envelope["meta"]["data_quality"] == "PARTIAL"


def test_the_cli_renders_a_human_summary() -> None:
    from oft.cli.commands.replay import _render

    rendered = _render(
        PaperLoopResult(
            run_id="paper-loop",
            trigger_id="TRG-1",
            decision_id="DEC-1",
            decision_validation="VALID",
            data_quality="PARTIAL",
            order=PaperOrderSummary(
                order_id="ORD-1", status="FILLED", client_order_id="oft-paper-BTCUSDT-entry-1"
            ),
            fill=PaperFillSummary(fill_id="FIL-1", price="70000.00", fee="1.00", source="PAPER"),
            position=PaperPositionSummary(position_id="POS-1", size="0.01", notional_usd="700.00"),
            summary_path="/tmp/summary.json",
        )
    )

    assert "deterministic=true" in rendered
    assert "data_quality=PARTIAL" in rendered
    assert "DEC-1" in rendered
    assert "70000.00" in rendered
    assert "/tmp/summary.json" in rendered


def test_the_cli_fails_clearly_when_a_recording_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        [
            "replay",
            "run",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(tmp_path / "empty"),
        ],
    )

    assert result.exit_code != 0
    assert "recorded LLM output" in result.output


def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str] | None = None) -> Any:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    args = [
        "replay",
        "run",
        "--archive-fixture-dir",
        str(ARCHIVE),
        "--recordings-dir",
        str(tmp_path / "recordings"),
    ]
    return CliRunner().invoke(cli_app, [*args, *(extra or [])])


def _error_envelope(result: Any) -> dict[str, Any]:
    output = result.output
    return json.loads(output[output.index("{") :])


def test_the_cli_emits_a_catalog_code_when_live_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, ["--environment", "live", "--json"])

    envelope = _error_envelope(result)
    assert result.exit_code == 2
    assert envelope["error"]["code"] == "LIVE_DISABLED"
    assert envelope["error"]["code"] in ERROR_CATALOG


def test_the_cli_emits_a_catalog_code_for_a_missing_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, ["--json"])

    envelope = _error_envelope(result)
    assert envelope["error"]["code"] == "DATA_UNAVAILABLE"
    assert "recorded LLM output" in str(envelope["error"]["message"])


def test_the_cli_reports_a_tampered_recording_as_invalid_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay_recordings: Path
) -> None:
    recordings = tmp_path / "recordings"
    shutil.copytree(replay_recordings, recordings)
    analysis = sorted((recordings / "ANALYSIS").glob("*.json"))[0]
    payload = json.loads(analysis.read_text(encoding="utf-8"))
    payload["raw_text"] = payload["raw_text"] + "tampered"
    analysis.write_text(json.dumps(payload), encoding="utf-8")

    result = _invoke(tmp_path, monkeypatch, ["--json"])

    envelope = _error_envelope(result)
    message = str(envelope["error"]["message"])
    assert envelope["error"]["code"] == "VALIDATION_FAILED"
    assert "tampered" in message.lower()
    assert "record one" not in message.lower()


def _fake_result(tmp_path: Path) -> PaperLoopResult:
    summary = tmp_path / "summary.json"
    summary.write_text("{}", encoding="utf-8")
    return PaperLoopResult(
        run_id="paper-loop",
        trigger_id="TRG-1",
        decision_id="DEC-1",
        decision_validation="VALID",
        data_quality="PARTIAL",
        order=PaperOrderSummary(
            order_id="ORD-1", status="FILLED", client_order_id="oft-paper-BTCUSDT-entry-1"
        ),
        fill=PaperFillSummary(fill_id="FIL-1", price="70000.00", fee="1.00", source="PAPER"),
        position=PaperPositionSummary(position_id="POS-1", size="0.01", notional_usd="700.00"),
        summary_path=str(summary),
    )


def test_the_cli_renders_the_default_success_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.replay.paper_loop import PaperLoopReplay

    monkeypatch.setattr(PaperLoopReplay, "run", lambda self: _fake_result(tmp_path))

    result = _invoke(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "deterministic=true" in result.output
    assert "DEC-1" in result.output


def test_the_cli_reports_a_replay_error_as_a_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.replay.harness import ReplayError
    from oft.replay.paper_loop import PaperLoopReplay

    def fail(self: PaperLoopReplay) -> PaperLoopResult:
        raise ReplayError("the fixture window is unusable")

    monkeypatch.setattr(PaperLoopReplay, "run", fail)

    result = _invoke(tmp_path, monkeypatch, ["--json"])

    assert result.exit_code == 2
    envelope = json.loads(result.stdout[result.stdout.index("{") :])
    assert envelope["error"]["message"] == "the fixture window is unusable"


@pytest.mark.parametrize("bad_args", [["--start", "not-a-date"], ["--end", "not-a-date"]])
def test_the_cli_rejects_a_bad_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_args: list[str]
) -> None:
    result = _invoke(tmp_path, monkeypatch, bad_args)

    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output


def test_the_cli_rejects_a_bad_or_naive_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _invoke(tmp_path, monkeypatch, ["--now", "not-a-time"])
    naive = _invoke(tmp_path, monkeypatch, ["--now", "2026-11-06T12:00:00"])

    assert bad.exit_code != 0
    assert "ISO-8601" in bad.output
    assert naive.exit_code != 0
    assert "timezone-aware" in naive.output


def test_an_unmapped_replay_error_code_fails_loudly() -> None:
    from oft.cli.commands.replay import _catalog_code
    from oft.replay.harness import ReplayError

    assert _catalog_code("LLM_RECORDING_MISSING") == "DATA_UNAVAILABLE"
    assert _catalog_code("LLM_RECORDING_INVALID") == "VALIDATION_FAILED"
    assert _catalog_code("ORDER_REJECTED") == "ORDER_REJECTED"

    with pytest.raises(ReplayError, match="no ERROR_CATALOG mapping"):
        _catalog_code("A_FUTURE_CODE_WITH_NO_ENTRY")


def test_the_cli_requires_an_archive_fixture_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        ["replay", "run", "--recordings-dir", str(tmp_path / "recordings")],
    )

    assert result.exit_code == 2
    assert "--archive-fixture-dir" in result.output


def test_the_cli_rejects_a_dot_or_path_like_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.replay.paper_loop import PaperLoopReplay

    def unexpected(self: PaperLoopReplay) -> PaperLoopResult:  # pragma: no cover - must not run
        raise AssertionError("the replay must not start for an unsafe run id")

    monkeypatch.setattr(PaperLoopReplay, "run", unexpected)

    for run_id in (".", "..", "a/b"):
        result = _invoke(tmp_path, monkeypatch, ["--run-id", run_id])
        assert result.exit_code == 2, result.output
        assert "run_id" in result.output


def _raise(error: Exception) -> object:
    def fail(self: object) -> PaperLoopResult:
        raise error

    return fail


@pytest.mark.parametrize(
    "error",
    [
        "LLMRecordingError",
        "EntryRejected",
        "ReplayNonDeterministicError",
        "ConfigError",
        "ValueError",
    ],
)
def test_the_cli_maps_every_replay_failure_to_a_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    from pathlib import Path as _Path

    from oft.application.entry_service import EntryRejected
    from oft.config import ConfigError
    from oft.replay.harness import ReplayNonDeterministicError
    from oft.replay.llm_source import LLMRecordingError
    from oft.replay.paper_loop import PaperLoopReplay

    instances: dict[str, Exception] = {
        "LLMRecordingError": LLMRecordingError("ENTRY_RECHECK", "a" * 64, _Path("/tmp")),
        "EntryRejected": EntryRejected("ORDER_REJECTED", "entry rejected"),
        "ReplayNonDeterministicError": ReplayNonDeterministicError("non-deterministic"),
        "ConfigError": ConfigError("bad config"),
        "ValueError": ValueError("bad value"),
    }
    monkeypatch.setattr(PaperLoopReplay, "run", _raise(instances[error]))

    result = _invoke(tmp_path, monkeypatch)

    assert result.exit_code == 2, result.output
    assert instances[error].args[0] in result.output or error in result.output


def test_the_cli_runs_the_position_management_scenario_as_json(
    tmp_path: Path,
    position_management_recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    result = CliRunner().invoke(
        cli_app,
        [
            "replay",
            "run",
            "--scenario",
            "position-management",
            "--archive-fixture-dir",
            str(ARCHIVE),
            "--recordings-dir",
            str(position_management_recordings),
            "--run-id",
            "cli-management",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout[result.stdout.index("{") :])
    data = envelope["data"]
    assert data["scenario"] == "position-management"
    assert data["deterministic"] is True
    assert data["trade_id"].startswith("TRD-")
    assert data["review_decision_id"].startswith("RVW-")
    assert data["statistics_status"] in {"DEFINED", "UNDEFINED"}
    assert Path(data["summary_path"]).is_file()
    assert envelope["meta"]["data_quality"] == data["data_quality"]


def test_the_cli_rejects_an_unknown_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, ["--scenario", "unknown", "--json"])

    assert result.exit_code == 2
    envelope = _error_envelope(result)
    assert envelope["error"]["code"] == "VALIDATION_FAILED"
    assert "unknown replay scenario" in str(envelope["error"]["message"])


def test_the_cli_rejects_a_non_4h_period_for_the_management_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(
        tmp_path,
        monkeypatch,
        ["--scenario", "position-management", "--period", "1h", "--json"],
    )

    assert result.exit_code == 2
    envelope = _error_envelope(result)
    assert envelope["error"]["code"] == "VALIDATION_FAILED"
    assert "4h" in str(envelope["error"]["message"])
