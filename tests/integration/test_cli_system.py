"""Integration: ``oft system info|logs`` and ``oft daemon restart`` diagnostics.

These tests exercise the CLI command tree in-process against a temporary data
directory. They cover the offline diagnostics, deterministic log filtering,
recursive redaction, the bounded follow loop, and the restart confirmation and
open-positions contract (F12 / ADR-0003 exit codes 0/1/2/5).
"""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import oft
from oft.bootstrap import build_container
from oft.cli.commands import daemon as daemon_module
from oft.cli.commands import system as system_module
from oft.cli.commands.daemon import log_path, pid_meta_path, pid_path, write_pid
from oft.cli.main import app
from oft.config import ConfigError, Settings

SENTINEL_OPENAI = "SENTINEL-OPENAI-KEY-DO-NOT-LEAK"
SENTINEL_BINANCE = "SENTINEL-BINANCE-SECRET-DO-NOT-LEAK"
SENTINEL_API_TOKEN = "SENTINEL-API-TOKEN-DO-NOT-LEAK"

_SECRET_ENV = (
    "OFT_OPENAI_API_KEY",
    "OFT_ANTHROPIC_API_KEY",
    "OFT_DEEPSEEK_API_KEY",
    "OFT_BINANCE_API_KEY",
    "OFT_BINANCE_API_SECRET",
    "OFT_API_TOKEN",
    "OFT_TELEGRAM_BOT_TOKEN",
)

# Ambient shell config must not make an assertion environment dependent.
_OTHER_ENV = (
    "OFT_SCAN_PERIOD",
    "OFT_SCAN_JITTER_SECONDS",
    "OFT_MANAGEMENT_CADENCE",
    "OFT_LLM_PROVIDER",
)


def _configure_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    for name in _SECRET_ENV + _OTHER_ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(env="test", data_dir=tmp_path / "data")


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _write_log(settings: Settings, records: list[dict[str, Any]]) -> Path:
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _record(stamp: str, level: str, event: str, **extra: Any) -> dict[str, Any]:
    return {"timestamp": stamp, "level": level, "event": event, **extra}


# ---------------------------------------------------------------------------
# system info
# ---------------------------------------------------------------------------


def test_system_info_json_works_offline_with_a_complete_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)

    result = _invoke("system", "info", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert set(payload) == {"ok", "data", "error", "meta"}
    assert payload["meta"]["provenance"] == "CALC"
    assert payload["meta"]["data_quality"] == "COMPLETE"
    assert payload["meta"]["request_id"].startswith("REQ-")

    data = payload["data"]
    assert data["version"] == oft.__version__
    assert data["environment"] == "test"
    assert data["paths"]["data_dir"] == str(settings.data_dir)
    assert data["paths"]["log_file"] == str(settings.data_dir / "logs" / "daemon.log")
    assert data["paths"]["pid_file"] == str(settings.data_dir / "run" / "daemon.pid")
    assert data["config_source"] == {
        "env_prefix": "OFT_",
        "env_file": None,
        "env_file_present": False,
    }
    assert data["daemon"] == {"state": "OFFLINE", "pid": None, "uptime_seconds": None}
    assert data["scheduler"]["period"] == "4h"
    assert data["scheduler"]["pending_period"] == ""
    assert data["scheduler"]["timezone"] == "UTC"
    assert data["scheduler"]["next_run_at"].endswith("Z")
    assert data["jobs"] == {"position_monitor": None, "position_manage": None}
    assert data["generated_at"].endswith("Z")


def test_system_info_reports_a_running_daemon_and_uptime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    try:
        result = _invoke("system", "info", "--json")
    finally:
        pid_path(settings).unlink(missing_ok=True)
        pid_meta_path(settings).unlink(missing_ok=True)

    assert result.exit_code == 0, result.output
    daemon = json.loads(result.stdout)["data"]["daemon"]
    assert daemon["state"] == "RUNNING"
    assert daemon["pid"] == os.getpid()
    assert isinstance(daemon["uptime_seconds"], int)
    assert daemon["uptime_seconds"] >= 0


def test_system_info_never_serializes_secret_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_OPENAI_API_KEY", SENTINEL_OPENAI)
    monkeypatch.setenv("OFT_BINANCE_API_SECRET", SENTINEL_BINANCE)
    monkeypatch.setenv("OFT_API_TOKEN", SENTINEL_API_TOKEN)

    for args in (("system", "info", "--json"), ("system", "info")):
        result = _invoke(*args)
        assert result.exit_code == 0, result.output
        combined = result.stdout + result.stderr
        assert SENTINEL_OPENAI not in combined
        assert SENTINEL_BINANCE not in combined
        assert SENTINEL_API_TOKEN not in combined
        for key_name in ("openai_api_key", "binance_api_secret", "api_token"):
            assert key_name not in combined.lower()


def test_system_info_config_source_never_echoes_env_file_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("# ENV-FILE-SENTINEL-CONTENT\nOFT_LOG_LEVEL=INFO\n", encoding="utf-8")

    result = _invoke("system", "info", "--json")

    assert result.exit_code == 0, result.output
    source = json.loads(result.stdout)["data"]["config_source"]
    assert source["env_file_present"] is True
    assert source["env_file"] == str(env_file.resolve())
    assert "ENV-FILE-SENTINEL-CONTENT" not in result.stdout


def test_system_info_human_output_is_labelled_and_honest_about_never(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = _invoke("system", "info")

    assert result.exit_code == 0, result.output
    output = result.stdout
    for label in (
        "Version",
        "Environment",
        "Data dir",
        "Log file",
        "PID file",
        "Config source",
        "Daemon",
        "Scheduler",
        "Jobs",
        "Generated at",
    ):
        assert label in output
    assert "OFFLINE" in output
    assert "never" in output.lower()
    assert "not available (daemon is offline)" in output


def test_system_info_config_failure_exits_five(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_LLM_PROVIDER", "bogus")

    result = _invoke("system", "info", "--json")

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_config_invalid_never_echoes_a_credential_in_an_invalid_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_LLM_OPENAI_BASE_URL", "https://user:SUPERSECRET@api.openai.com")

    for args in (
        ("system", "info", "--json"),
        ("system", "logs", "--json"),
        ("daemon", "restart", "--yes", "--json"),
    ):
        result = _invoke(*args)
        combined = result.stdout + result.stderr
        assert result.exit_code == 5, result.output
        assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"
        assert "SUPERSECRET" not in combined
        assert "user:" not in combined


# ---------------------------------------------------------------------------
# system logs
# ---------------------------------------------------------------------------


def test_system_logs_since_absolute_and_level_filter_deterministically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(
        settings,
        [
            _record("2026-10-08T12:00:00.000Z", "info", "scan_started"),
            _record("2026-10-08T12:05:00.000Z", "warning", "trigger_due"),
            _record("2026-10-08T12:10:00.000Z", "error", "management_failed"),
        ],
    )

    since = _invoke("system", "logs", "--since", "2026-10-08T12:04:00.000Z", "--json")
    assert since.exit_code == 0, since.output
    since_payload = json.loads(since.stdout)
    since_data = since_payload["data"]
    assert [line["event"] for line in since_data["lines"]] == [
        "trigger_due",
        "management_failed",
    ]
    assert since_data["matched"] == 2
    assert since_data["dropped"] == 0
    assert since_payload["meta"]["data_quality"] == "COMPLETE"

    level = _invoke("system", "logs", "--level", "warning", "--json")
    assert level.exit_code == 0, level.output
    level_data = json.loads(level.stdout)["data"]
    assert [line["event"] for line in level_data["lines"]] == [
        "trigger_due",
        "management_failed",
    ]


def test_system_logs_relative_since_uses_the_clock_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(
        settings,
        [
            _record("2026-10-08T12:00:00.000Z", "info", "old_event"),
            _record("2026-10-08T12:05:00.000Z", "info", "boundary_event"),
            _record("2026-10-08T12:10:00.000Z", "info", "recent_event"),
        ],
    )
    fixed = datetime(2026, 10, 8, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(system_module, "_now", lambda _settings: fixed)

    result = _invoke("system", "logs", "--since", "10m", "--json")

    assert result.exit_code == 0, result.output
    lines = json.loads(result.stdout)["data"]["lines"]
    assert [line["event"] for line in lines] == ["boundary_event", "recent_event"]


def test_system_logs_invalid_level_and_since_exit_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(settings, [_record("2026-10-08T12:00:00.000Z", "info", "scan_started")])

    bad_level = _invoke("system", "logs", "--level", "verbose", "--json")
    assert bad_level.exit_code == 2, bad_level.output
    assert json.loads(bad_level.stdout)["error"]["code"] == "VALIDATION_FAILED"

    bad_since = _invoke("system", "logs", "--since", "yesterday-ish", "--json")
    assert bad_since.exit_code == 2, bad_since.output
    assert json.loads(bad_since.stdout)["error"]["code"] == "VALIDATION_FAILED"


def test_system_logs_drops_unparsable_lines_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "not-json-root",
                json.dumps(_record("2026-10-08T12:10:00.000Z", "info", "kept")),
                json.dumps({"level": "error", "event": "no_timestamp"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("system", "logs", "--since", "2026-10-08T12:00:00.000Z", "--json")

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [line["event"] for line in data["lines"]] == ["kept"]
    assert data["dropped"] == 2
    assert result.stderr.count("warning:") == 1
    assert "dropped" in result.stderr.lower()


def test_system_logs_redacts_keys_tokens_cookies_and_prompt_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        _record(
            "2026-10-08T12:00:00.000Z",
            "info",
            "scan_completed",
            scan_id="SCAN-KEEP-1",
            token="tok-SENTINEL",
            api_key="ak-SENTINEL",
            cookie="cookie-SENTINEL",
        ),
        _record(
            "2026-10-08T12:01:00.000Z",
            "info",
            "llm_call",
            prompt_id="analysis",
            prompt_version=9,
            prompt="PROMPT-SENTINEL",
            system_prompt="SYSTEM-PROMPT-SENTINEL",
        ),
        _record(
            "2026-10-08T12:02:00.000Z",
            "info",
            "http_call",
            message="Authorization Bearer BEARER-SENTINEL and cookie: COOKIE-TEXT-SENTINEL",
            secret_value="SECRET-SENTINEL",
        ),
        {
            "timestamp": "2026-10-08T12:03:00.000Z",
            "level": "info",
            "event": "raw",
            "message": "sk-ABCDEFGH12345678 ghp_ABCDEFGHIJKLMNOPQRSTUV and AKIAABCDEFGHIJKLMNOP",
        },
    ]
    raw = "raw line with Bearer RAW-BEARER-SENTINEL"
    path.write_text(
        "\n".join([json.dumps(record) for record in records] + [raw]) + "\n",
        encoding="utf-8",
    )

    human = _invoke("system", "logs")
    assert human.exit_code == 0, human.output
    human_text = human.stdout

    json_result = _invoke("system", "logs", "--json")
    assert json_result.exit_code == 0, json_result.output
    json_text = json_result.stdout

    for text in (human_text, json_text):
        for forbidden in (
            "tok-SENTINEL",
            "ak-SENTINEL",
            "cookie-SENTINEL",
            "PROMPT-SENTINEL",
            "SYSTEM-PROMPT-SENTINEL",
            "BEARER-SENTINEL",
            "COOKIE-TEXT-SENTINEL",
            "SECRET-SENTINEL",
            "sk-ABCDEFGH12345678",
            "ghp_ABCDEFGHIJKLMNOPQRSTUV",
            "AKIAABCDEFGHIJKLMNOP",
            "RAW-BEARER-SENTINEL",
        ):
            assert forbidden not in text
        # Redaction is surgical: ordinary ids/events and prompt metadata survive.
        assert "SCAN-KEEP-1" in text
        assert "scan_completed" in text
        assert "analysis" in text
        assert "prompt_version" in text

    lines = json.loads(json_result.stdout)["data"]["lines"]
    by_event = {line["event"]: line for line in lines if isinstance(line, dict)}
    assert by_event["llm_call"]["prompt_id"] == "analysis"
    assert by_event["llm_call"]["prompt_version"] == 9
    assert by_event["llm_call"]["prompt"] == "[REDACTED UNTRUSTED PROMPT]"
    assert by_event["scan_completed"]["scan_id"] == "SCAN-KEEP-1"


def test_system_logs_missing_file_exits_zero_honestly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    human = _invoke("system", "logs")
    assert human.exit_code == 0, human.output
    assert "no system log" in human.stdout.lower()

    as_json = _invoke("system", "logs", "--json")
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)["data"]
    assert data["lines"] == []
    assert data["matched"] == 0
    assert data["path"].endswith("logs/daemon.log")


def test_system_logs_follow_prints_initial_tail_then_new_lines_and_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_record("2026-10-08T12:00:00.000Z", "info", "initial_event")) + "\n",
        encoding="utf-8",
    )
    appended = _record(
        "2026-10-08T12:01:00.000Z", "info", "appended_event", token="tok-FOLLOW-SENTINEL"
    )
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(appended) + "\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--lines", "1")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "initial_event" in result.stdout
    assert "appended_event" in result.stdout
    assert "tok-FOLLOW-SENTINEL" not in result.output


def test_system_logs_follow_json_emits_ndjson(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_record("2026-10-08T12:00:00.000Z", "info", "initial_event")) + "\n",
        encoding="utf-8",
    )
    appended = _record(
        "2026-10-08T12:01:00.000Z", "info", "appended_event", cookie="cookie-SENTINEL"
    )
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(appended) + "\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--json", "--lines", "1")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    documents = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(documents) == 2
    assert documents[0]["event"] == "initial_event"
    assert documents[1]["event"] == "appended_event"
    assert "cookie-SENTINEL" not in result.stdout


def test_system_logs_plain_lines_keep_exactly_one_terminator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Leading spaces stay; only the line terminator is removed.
    path.write_text("  first plain\nsecond plain\n", encoding="utf-8")

    human = _invoke("system", "logs")
    assert human.exit_code == 0, human.output
    assert human.stdout == "  first plain\nsecond plain\n"

    as_json = _invoke("system", "logs", "--json")
    assert as_json.exit_code == 0, as_json.output
    lines = json.loads(as_json.stdout)["data"]["lines"]
    assert lines == ["  first plain", "second plain"]


def test_system_logs_follow_json_plain_lines_are_clean_ndjson(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("initial plain\n", encoding="utf-8")
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("appended plain\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--json", "--lines", "1")

    assert result.exit_code == 0, result.output
    documents = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    # No JSON string may carry the raw ``\n`` terminator.
    assert documents == ["initial plain", "appended plain"]


def test_system_logs_follow_on_a_directory_exits_one_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.mkdir(parents=True)

    result = _invoke("system", "logs", "--follow", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "DATA_UNAVAILABLE"


def test_system_logs_follow_permission_error_exits_one_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(settings, [_record("2026-10-08T12:00:00.000Z", "info", "present")])

    def raise_permission(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", raise_permission)

    result = _invoke("system", "logs", "--follow", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "DATA_UNAVAILABLE"


def test_system_logs_follow_handles_truncation_and_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_record("2026-10-08T12:00:00.000Z", "info", "initial_event_with_a_long_name"))
        + "\n",
        encoding="utf-8",
    )
    truncated = _record("2026-10-08T12:01:00.000Z", "info", "trunc")
    rotated = _record("2026-10-08T12:02:00.000Z", "info", "rotated")
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            # Shrink the file in place: the reader must restart at offset 0.
            path.write_text(json.dumps(truncated) + "\n", encoding="utf-8")
            return
        if calls["count"] == 2:
            # Replace the inode: the reader must also restart at offset 0.
            replacement = path.with_suffix(".new")
            replacement.write_text(json.dumps(rotated) + "\n", encoding="utf-8")
            os.replace(replacement, path)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--lines", "1")

    assert result.exit_code == 0, result.output
    assert "initial_event_with_a_long_name" in result.stdout
    assert "trunc" in result.stdout
    assert "rotated" in result.stdout


def test_system_logs_lines_bounds_the_tail_but_matched_counts_every_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(
        settings,
        [
            _record("2026-10-08T12:01:00.000Z", "info", "one"),
            _record("2026-10-08T12:02:00.000Z", "info", "two"),
            _record("2026-10-08T12:03:00.000Z", "info", "three"),
        ],
    )

    result = _invoke(
        "system", "logs", "--since", "2026-10-08T12:00:00.000Z", "--lines", "1", "--json"
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["matched"] == 3
    assert data["returned"] == 1
    assert [line["event"] for line in data["lines"]] == ["three"]


def test_system_logs_empty_filter_result_exits_zero_with_a_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _write_log(settings, [_record("2026-10-08T12:00:00.000Z", "info", "old")])

    as_json = _invoke("system", "logs", "--since", "2026-10-08T13:00:00.000Z", "--json")
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)["data"]
    assert data["lines"] == []
    assert data["matched"] == 0
    assert "no log lines matched --since/--level" in data["cause"]

    human = _invoke("system", "logs", "--since", "2026-10-08T13:00:00.000Z")
    assert human.exit_code == 0, human.output
    assert "no log lines matched --since/--level" in human.stdout


# ---------------------------------------------------------------------------
# daemon restart
# ---------------------------------------------------------------------------


def _seed_open_position(settings: Settings, position_id: str = "POS-RESTART-1") -> None:
    container = build_container(settings, initialize=True)
    with sqlite3.connect(container.store.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_accounts "
            "(account_id, environment, currency, initial_balance, balance, updated_at) "
            "VALUES ('ACC-RESTART', 'PAPER', 'USDT', '10000.00', '10000.00', "
            "'2026-10-08T12:00:00.000Z')"
        )
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, 'ACC-RESTART', 'PAPER', 'BTCUSDT', 'OPEN', "
            "'2026-10-08T12:00:00.000Z', '{}')",
            (position_id,),
        )
        connection.commit()


def test_daemon_restart_without_a_live_daemon_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = _invoke("daemon", "restart", "--yes", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "DAEMON_NOT_RUNNING"
    assert "oft daemon start" in payload["error"]["message"]


def test_daemon_restart_refuses_non_interactive_without_yes_and_leaves_daemon_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    before = pid_path(settings).read_text(encoding="utf-8")
    try:
        result = _invoke("daemon", "restart", "--json")

        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout)["error"]["code"] == "VALIDATION_FAILED"
        assert "--yes" in result.stdout
        assert pid_path(settings).read_text(encoding="utf-8") == before
    finally:
        pid_path(settings).unlink(missing_ok=True)
        pid_meta_path(settings).unlink(missing_ok=True)


def test_daemon_restart_yes_warns_and_preserves_open_positions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    _seed_open_position(settings)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 22222)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)

    try:
        result = _invoke("daemon", "restart", "--yes")

        assert result.exit_code == 0, result.output
        output = result.stdout
        assert "Open positions" in output
        assert "1 open position remains open" in output
        assert "POS-RESTART-1" in output
        assert "restarted daemon" in output
        assert f"pid {os.getpid()}" in output
        assert "22222" in output

        positions = build_container(settings).store.list_positions(state="OPEN")
        assert [position.position_id for position in positions] == ["POS-RESTART-1"]
    finally:
        pid_path(settings).unlink(missing_ok=True)
        pid_meta_path(settings).unlink(missing_ok=True)


def test_daemon_restart_json_keeps_stdout_a_clean_envelope_and_warns_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    _seed_open_position(settings)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 33333)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)

    try:
        result = _invoke("daemon", "restart", "--yes", "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["data"] == {
            "state": "RESTARTED",
            "previous_pid": os.getpid(),
            "pid": 33333,
            "open_positions": 1,
        }
        assert "Open positions" in result.stderr
        assert "1 open position remains open" in result.stderr
        assert "warning:" not in result.stdout.lower()
    finally:
        pid_path(settings).unlink(missing_ok=True)
        pid_meta_path(settings).unlink(missing_ok=True)


def test_daemon_restart_config_failure_exits_five(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_LLM_PROVIDER", "bogus")

    result = _invoke("daemon", "restart", "--json")

    assert result.exit_code == 5, result.output
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"


# ---------------------------------------------------------------------------
# redaction edge cases
# ---------------------------------------------------------------------------


def test_redact_record_redacts_multi_pair_cookies_in_text_and_dict_value() -> None:
    redacted = system_module.redact_record(
        {
            "headers": "Cookie: a=1; b=SECRET-MULTI; c=3",
            "message": "Set-Cookie: sid=abc; token=SECRET-SET",
            "cookie": "a=1; b=SECRET-DICT",
            "set-cookie": "sid=xyz; token=SECRET-SET-DICT",
        }
    )

    text = json.dumps(redacted)
    for forbidden in (
        "SECRET-MULTI",
        "SECRET-SET",
        "SECRET-DICT",
        "SECRET-SET-DICT",
    ):
        assert forbidden not in text
    assert redacted["headers"] == "Cookie: [REDACTED]"
    assert redacted["message"] == "Set-Cookie: [REDACTED]"
    assert redacted["cookie"] == "[REDACTED]"
    assert redacted["set-cookie"] == "[REDACTED]"


def test_redact_record_redacts_authorization_and_api_key_variants() -> None:
    redacted = system_module.redact_record(
        {
            "message": "Authorization: Basic dXNlcjpwYXNz",
            "detail": "api_key=SECRET-FREE-TEXT and api-key=SECRET-FREE-TEXT-2",
            "apikey": "AK-SECRET",
            "X-Api-Key": "XAK-SECRET",
            "api_key": "UNDERSCORE-SECRET",
            "authorization": "Digest abc-SECRET",
            "scan_id": "SCAN-KEEP",
            "event": "http_call",
            "prompt_id": "analysis",
            "prompt_version": 3,
        }
    )

    text = json.dumps(redacted)
    for forbidden in (
        "dXNlcjpwYXNz",
        "SECRET-FREE-TEXT",
        "SECRET-FREE-TEXT-2",
        "AK-SECRET",
        "XAK-SECRET",
        "UNDERSCORE-SECRET",
        "abc-SECRET",
    ):
        assert forbidden not in text
    assert redacted["message"] == "Authorization: [REDACTED]"
    assert redacted["detail"] == "api_key=[REDACTED] and api_key=[REDACTED]"
    # Ordinary ids/events and prompt metadata are not over-redacted.
    assert redacted["scan_id"] == "SCAN-KEEP"
    assert redacted["event"] == "http_call"
    assert redacted["prompt_id"] == "analysis"
    assert redacted["prompt_version"] == 3


def test_redact_record_redacts_secrets_nested_in_lists() -> None:
    redacted = system_module.redact_record(
        {
            "event": "batch",
            "items": [
                {"token": "TOKEN-LIST-SECRET", "scan_id": "SCAN-LIST-KEEP"},
                {"nested": [{"password": "PASSWORD-LIST-SECRET"}]},
                "api_key=APIKEY-LIST-SECRET",
            ],
        }
    )

    text = json.dumps(redacted)
    for forbidden in (
        "TOKEN-LIST-SECRET",
        "PASSWORD-LIST-SECRET",
        "APIKEY-LIST-SECRET",
    ):
        assert forbidden not in text
    assert redacted["items"][0]["scan_id"] == "SCAN-LIST-KEEP"
    assert redacted["items"][2] == "api_key=[REDACTED]"


def test_redact_record_redacts_new_prompt_body_keys_without_over_redacting_content() -> None:
    for key in ("response", "completion", "output", "content", "input", "body"):
        assert (
            system_module.redact_record({key: "BODY-SECRET"})[key] == system_module.PROMPT_REDACTED
        )
    assert system_module.redact_record({"summary": "keep"})["summary"] == "keep"


@pytest.mark.parametrize(
    "key",
    [
        "systemPrompt",
        "userPrompt",
        "responseBody",
        "input",
        "content",
        "body",
        "raw_response",
        "llm_request",
        "llm_response",
    ],
)
def test_redact_record_normalizes_prompt_body_aliases(key: str) -> None:
    redacted = system_module.redact_record({key: "BODY-ALIAS-SECRET"})

    assert redacted[key] == system_module.PROMPT_REDACTED
    assert "BODY-ALIAS-SECRET" not in json.dumps(redacted)


@pytest.mark.parametrize(
    "key",
    [
        "prompt_text",
        "promptContent",
        "promptContentText",
        "completion_text",
        "completionContent",
        "reasoning",
        "reasoning_content",
        "reasoningBody",
        "thinking",
        "answer",
        "Prompt-Text",
    ],
)
def test_redact_record_redacts_prompt_body_residual_aliases(key: str) -> None:
    redacted = system_module.redact_record(
        {key: "PROMPT-ALIAS-SECRET", "prompt_id": "analysis", "prompt_version": 4}
    )

    assert redacted["prompt_id"] == "analysis"
    assert redacted["prompt_version"] == 4
    assert redacted[key] == system_module.PROMPT_REDACTED
    assert "PROMPT-ALIAS-SECRET" not in json.dumps(redacted)


def test_redact_record_keeps_prompt_metadata_but_redacts_a_secret_key() -> None:
    redacted = system_module.redact_record(
        {
            "prompt_id": "analysis",
            "prompt_version": 7,
            "sk-ABCDEFGH12345678": "value",
        }
    )

    text = json.dumps(redacted)
    assert '"prompt_id": "analysis"' in text
    assert '"prompt_version": 7' in text
    assert "sk-ABCDEFGH12345678" not in text


def test_redact_record_redacts_free_text_assignments_and_telegram_tokens() -> None:
    redacted = system_module.redact_record(
        {
            "password": "password=hunter2",
            "client_secret": "client_secret=CLIENT-SECRET",
            "access_token": "access_token=ACCESS-SECRET",
            "signature": "signature=SIG-SECRET",
            "prefixed_key": "my_api_key=MYAPI-SECRET",
            "cookie": "cookie=a=1; b=COOKIE-SECRET",
            "telegram": "bot 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        }
    )

    text = json.dumps(redacted)
    for forbidden in (
        "hunter2",
        "CLIENT-SECRET",
        "ACCESS-SECRET",
        "SIG-SECRET",
        "MYAPI-SECRET",
        "COOKIE-SECRET",
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    ):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        ("token=ab", "token=[REDACTED]"),
        ("secret=x", "secret=[REDACTED]"),
        ("TOKEN: 1", "TOKEN=[REDACTED]"),
        ("Secret=short", "Secret=[REDACTED]"),
        ("token=longer-value", "token=[REDACTED]"),
    ],
)
def test_redact_record_redacts_bare_short_secret_and_token_assignments(
    prose: str, expected: str
) -> None:
    # The assignment pattern matches any value length (not just 8+).
    redacted = system_module.redact_record({"message": f"prefix {prose} suffix"})

    assert redacted["message"] == f"prefix {expected} suffix"


def test_redact_record_redacts_configured_secret_values_in_free_text() -> None:
    sentinel = "CONFIGURED-SENTINEL-VALUE-DO-NOT-LEAK"

    redacted = system_module.redact_record(
        {"message": f"the key is {sentinel} in prose", "scan_id": "SCAN-KEEP"},
        secret_values=(sentinel,),
    )

    text = json.dumps(redacted)
    assert sentinel not in text
    assert redacted["scan_id"] == "SCAN-KEEP"


@pytest.mark.parametrize(
    "prose",
    [
        "pass\x01word=hunter2",
        "tok\x01en=SPLIT-KEYWORD-SECRET",
        "Bear\x01er ABCDEFGHIJKLMNOP",
        "pass​word=FORMAT-CONTROL-SECRET",
    ],
)
def test_redact_record_strips_controls_before_matching_split_keywords(prose: str) -> None:
    # Stripping after redaction re-joins the split keyword into a visible secret.
    redacted = system_module.redact_record(
        {"message": f"before {prose} after", "scan_id": "SCAN-CONTROL-KEEP"}
    )

    text = json.dumps(redacted)
    for forbidden in (
        "hunter2",
        "SPLIT-KEYWORD-SECRET",
        "ABCDEFGHIJKLMNOP",
        "FORMAT-CONTROL-SECRET",
    ):
        assert forbidden not in text
    assert redacted["scan_id"] == "SCAN-CONTROL-KEEP"
    assert "before" in redacted["message"]
    assert "after" in redacted["message"]


def test_redact_record_strips_a_control_inserted_into_a_configured_secret() -> None:
    secret = "SUPERSECRET123"

    redacted = system_module.redact_record(
        {"message": "value SUPER\x01SECRET123 echoed", "scan_id": "SCAN-KEEP"},
        secret_values=(secret,),
    )

    text = json.dumps(redacted)
    assert secret not in text
    assert "SUPER\x01" not in text
    assert redacted["scan_id"] == "SCAN-KEEP"


@pytest.mark.parametrize(
    ("prose", "forbidden"),
    [
        ("pass­word=hunter2", "hunter2"),
        ("tok؜en=ALM-SECRET", "ALM-SECRET"),
        ("pass᠎word=MVS-SECRET", "MVS-SECRET"),
        ("sk-️ABC12345", "ABC12345"),
        ("Bearer ABC​DEF", "ABCDEF"),
    ],
)
def test_redact_record_strips_unicode_format_controls_before_matching(
    prose: str, forbidden: str
) -> None:
    # Soft hyphen, ALM, MVS, variation selectors, and zero-width spaces must not
    # let a keyword or token split past every redaction pattern.
    redacted = system_module.redact_record({"message": f"before {prose} after"})

    text = json.dumps(redacted)
    assert forbidden not in text
    assert "before" in redacted["message"]
    assert "after" in redacted["message"]


def test_redact_record_strips_an_invisible_mark_inserted_into_a_configured_secret() -> None:
    secret = "SUPERSECRET123"

    redacted = system_module.redact_record(
        {"message": "value SUPER­SECRET123 echoed", "scan_id": "SCAN-KEEP"},
        secret_values=(secret,),
    )

    text = json.dumps(redacted)
    assert secret not in text
    assert "SECRET123" not in text
    assert redacted["scan_id"] == "SCAN-KEEP"


def test_redact_record_strips_cgj_and_preserves_ordinary_text() -> None:
    cgj = "͏"
    redacted = system_module.redact_record(
        {
            "message": f"scan{cgj}_id=SCAN-KEEP",
            "event": f"scan{cgj}_started",
            "label": "扫描 café",  # non-Latin letters survive
            "combining": "café",  # an ordinary Mn combining mark survives
        }
    )

    assert redacted["message"] == "scan_id=SCAN-KEEP"
    assert redacted["event"] == "scan_started"
    assert redacted["label"] == "扫描 café"
    assert redacted["combining"] == "café"
    assert "SCAN-KEEP" in redacted["message"]


def test_redact_record_preserves_tabs_and_newlines_but_strips_carriage_returns() -> None:
    redacted = system_module.redact_record({"message": "plain\ttext\nnext\rline"})

    assert redacted["message"] == "plain\ttext\nnextline"


@pytest.mark.parametrize(
    "prose",
    [
        'api_key="abc def"',
        "x-api-key: 'abc def'",
        "my_api_key=`abc def`",
    ],
)
def test_redact_record_redacts_quoted_multi_word_api_key_values(prose: str) -> None:
    redacted = system_module.redact_record({"message": f"prefix {prose} suffix"})

    assert "abc def" not in redacted["message"]
    assert "api_key=[REDACTED]" in redacted["message"]
    assert "prefix" in redacted["message"]
    assert "suffix" in redacted["message"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("signature", "SIGNATURE-VALUE-SECRET"),
        ("x-signature", "X-SIGNATURE-VALUE-SECRET"),
        ("app_signature", "APP-SIGNATURE-VALUE-SECRET"),
    ],
)
def test_redact_record_redacts_structured_signature_keys(key: str, value: str) -> None:
    redacted = system_module.redact_record({key: value, "event": "signed", "scan_id": "SCAN-KEEP"})

    assert redacted[key] == system_module.REDACTED
    assert value not in json.dumps(redacted)
    assert redacted["event"] == "signed"
    assert redacted["scan_id"] == "SCAN-KEEP"


@pytest.mark.parametrize(
    ("prose", "forbidden"),
    [
        ("db_password=DBPASS-SECRET", "DBPASS-SECRET"),
        ("DB_PASSWORD=UPPER-DBPASS-SECRET", "UPPER-DBPASS-SECRET"),
        ("user_token=USERTOK-SECRET", "USERTOK-SECRET"),
        ("db_access_token=ACCESSTOK-SECRET", "ACCESSTOK-SECRET"),
        ("app_signature=SIGVAL-SECRET", "SIGVAL-SECRET"),
    ],
)
def test_redact_record_redacts_underscored_free_text_keys(prose: str, forbidden: str) -> None:
    redacted = system_module.redact_record(
        {"message": f"prefix {prose} suffix", "event": "http_call", "prompt_id": "analysis"}
    )

    assert forbidden not in json.dumps(redacted)
    assert redacted["event"] == "http_call"
    assert redacted["prompt_id"] == "analysis"


@pytest.mark.parametrize(
    ("prose", "forbidden"),
    [
        ('secret="abc def"', "abc def"),
        ("password='hunter 2'", "hunter 2"),
        ("token=`back tick value`", "back tick value"),
    ],
)
def test_redact_record_redacts_quoted_multi_word_assignment_values(
    prose: str, forbidden: str
) -> None:
    redacted = system_module.redact_record({"message": f"prefix {prose} suffix"})

    assert forbidden not in redacted["message"]
    assert "[REDACTED]" in redacted["message"]


@pytest.mark.parametrize(
    "token",
    [
        "gho_" + "A" * 24,
        "ghu_" + "B" * 24,
        "ghs_" + "C" * 24,
        "ghr_" + "D" * 24,
        "github_pat_" + "E" * 24,
    ],
)
def test_redact_record_redacts_github_token_shapes(token: str) -> None:
    redacted = system_module.redact_record({"message": f"token {token} here"})

    assert token not in json.dumps(redacted)
    assert "[REDACTED]" in redacted["message"]


@pytest.mark.parametrize(
    "key",
    [
        "response_text",
        "response_content",
        "raw_completion",
        "thinking_content",
        "thinking_text",
        "answer_text",
        "answer_content",
        "content_text",
    ],
)
def test_redact_record_redacts_new_response_body_aliases(key: str) -> None:
    redacted = system_module.redact_record(
        {key: "RESPONSE-BODY-SECRET", "prompt_id": "analysis", "prompt_version": 5}
    )

    assert redacted[key] == system_module.PROMPT_REDACTED
    assert redacted["prompt_id"] == "analysis"
    assert redacted["prompt_version"] == 5
    assert "RESPONSE-BODY-SECRET" not in json.dumps(redacted)


def test_system_logs_redacts_a_configured_secret_echoed_in_free_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_OPENAI_API_KEY", SENTINEL_OPENAI)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _record(
                "2026-10-08T12:00:00.000Z",
                "info",
                "leak",
                message=f"configured value {SENTINEL_OPENAI} echoed in prose",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("system", "logs", "--json")

    assert result.exit_code == 0, result.output
    assert SENTINEL_OPENAI not in result.stdout
    assert "leak" in result.stdout


def test_system_logs_strips_control_characters_from_plain_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "plain \x1b[31mred\x1b[0m \x07bel \x1b]0;title\x07 done\n",
        encoding="utf-8",
    )

    result = _invoke("system", "logs")

    assert result.exit_code == 0, result.output
    assert "plain" in result.stdout
    assert "done" in result.stdout
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout


# ---------------------------------------------------------------------------
# system logs: data quality and I/O errors
# ---------------------------------------------------------------------------


def test_system_logs_json_marks_partial_data_quality_when_lines_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "not-json-root",
                json.dumps(_record("2026-10-08T12:10:00.000Z", "info", "kept")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("system", "logs", "--since", "2026-10-08T12:00:00.000Z", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["dropped"] == 1
    assert payload["meta"]["data_quality"] == "PARTIAL"


def test_system_logs_io_error_exits_one_with_data_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    def raise_permission(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", raise_permission)

    as_json = _invoke("system", "logs", "--json")
    assert as_json.exit_code == 1, as_json.output
    assert json.loads(as_json.stdout)["error"]["code"] == "DATA_UNAVAILABLE"

    human = _invoke("system", "logs")
    assert human.exit_code == 1, human.output
    assert "could not read system log" in human.stderr


# ---------------------------------------------------------------------------
# daemon restart internals
# ---------------------------------------------------------------------------


def _restart_settings(tmp_path: Path) -> Settings:
    return Settings(env="test", data_dir=tmp_path / "data")


def _seed_pid_files(settings: Settings, pid: int = 12345) -> None:
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    ("waitpid_result", "expected"),
    [((123, 0), True), ((0, 0), False)],
)
def test_reap_child_normal(
    monkeypatch: pytest.MonkeyPatch, waitpid_result: tuple[int, int], expected: bool
) -> None:
    monkeypatch.setattr(daemon_module.os, "waitpid", lambda _pid, _options: waitpid_result)

    assert daemon_module._reap_child(123) is expected


@pytest.mark.parametrize("error", [ChildProcessError, OSError])
def test_reap_child_waitpid_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    def raise_error(_pid: int, _options: int) -> tuple[int, int]:
        raise error

    monkeypatch.setattr(daemon_module.os, "waitpid", raise_error)

    assert daemon_module._reap_child(123) is False


def test_stop_daemon_normal_exit_removes_pid_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daemon_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(daemon_module, "_reap_child", lambda _pid: True)

    assert daemon_module._stop_daemon(settings, 12345, timeout_seconds=1.0) is True
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_stop_daemon_process_lookup_removes_pid_and_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)

    def raise_lookup(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(daemon_module.os, "kill", raise_lookup)

    assert daemon_module._stop_daemon(settings, 12345, timeout_seconds=1.0) is True
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_stop_daemon_timeout_never_force_kills_and_keeps_pid_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daemon_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(daemon_module, "_reap_child", lambda _pid: False)
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)

    assert daemon_module._stop_daemon(settings, 12345, timeout_seconds=0.001) is False
    # The daemon may still be alive: keep the artifacts so stop/status can retry.
    assert pid_path(settings).exists()
    assert pid_meta_path(settings).exists()


def test_stop_daemon_refuses_when_the_pid_fence_changes_before_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    # The caller's earlier check matched, but the PID was recycled (or the meta
    # vanished) before ``_stop_daemon``: the re-check must refuse without
    # ``os.kill``.
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: False)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert daemon_module._stop_daemon(settings, 12345, timeout_seconds=0.001) is False
    assert kills == []


def test_stop_daemon_never_signals_a_non_positive_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    for pid in (0, -1):
        assert daemon_module._stop_daemon(settings, pid, timeout_seconds=0.001) is False

    assert kills == []


def test_daemon_stop_rechecks_the_fence_before_signalling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text("12345", encoding="utf-8")
    matches = iter([True, False])
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: next(matches))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert kills == []


def test_daemon_stop_signals_and_removes_both_pid_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "STOPPED", "pid": 12345}
    assert kills == [(12345, daemon_module.signal.SIGTERM)]
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_daemon_stop_process_lookup_emits_not_running_and_removes_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)

    def raise_lookup(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(daemon_module.os, "kill", raise_lookup)

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_daemon_restart_refuses_when_the_fence_changes_before_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    # First check (in ``daemon_restart``) matches; the re-check inside
    # ``_stop_daemon`` does not, so the restart must not signal.
    matches = iter([True, False])
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: next(matches))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "INTERNAL_ERROR"
    assert kills == []


class _RecordingPopen:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def test_spawn_detached_daemon_uses_the_module_entrypoint_and_private_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", _RecordingPopen)

    child = daemon_module._spawn_detached_daemon(settings)

    assert isinstance(child, _RecordingPopen)
    assert child.args == ([sys.executable, "-m", "oft.cli.main", "daemon", "start"],)
    kwargs = child.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    # structlog already writes daemon.log: stdout is discarded and only the
    # low-volume stderr stream is captured for a startup-failure tail.
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    log = daemon_module._restart_stderr_path(settings)
    assert kwargs["stderr"].name == str(log)
    assert kwargs["stderr"].closed is True
    assert (log.stat().st_mode & 0o777) == 0o600


def test_cli_main_module_entrypoint_help_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "oft.cli.main", "system", "info", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Show version" in completed.stdout


class _FakeChild:
    def __init__(self, *, poll_result: int | None, wait_timeout_first: bool = False) -> None:
        self._poll_result = poll_result
        self._wait_timeout_first = wait_timeout_first
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self._poll_result

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._wait_timeout_first and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


def test_terminate_child_is_a_noop_when_already_exited() -> None:
    child = _FakeChild(poll_result=0)

    daemon_module._terminate_child(child)  # type: ignore[arg-type]

    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert child.wait_calls == 0


def test_terminate_child_terminates_a_running_process() -> None:
    child = _FakeChild(poll_result=None)

    daemon_module._terminate_child(child)  # type: ignore[arg-type]

    assert child.terminate_calls == 1
    assert child.kill_calls == 0
    assert child.wait_calls == 1


def test_terminate_child_kills_after_a_terminate_timeout() -> None:
    child = _FakeChild(poll_result=None, wait_timeout_first=True)

    daemon_module._terminate_child(child)  # type: ignore[arg-type]

    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert child.wait_calls == 2


def test_wait_for_new_pid_returns_the_new_live_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setattr(daemon_module, "read_pid", lambda _settings: 222)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda _settings, _pid: True)

    assert daemon_module._wait_for_new_pid(settings, 111, timeout_seconds=1.0) == 222


def test_wait_for_new_pid_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setattr(daemon_module, "read_pid", lambda _settings: 111)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda _settings, _pid: True)

    assert daemon_module._wait_for_new_pid(settings, 111, timeout_seconds=0.001) is None


def test_wait_for_new_pid_fails_fast_when_the_child_already_exited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    reads = {"count": 0}

    def read_pid(_settings: Settings) -> int:
        reads["count"] += 1
        return 111

    monkeypatch.setattr(daemon_module, "read_pid", read_pid)
    started = time.monotonic()

    assert (
        daemon_module._wait_for_new_pid(
            settings,
            111,
            timeout_seconds=30.0,
            child=_FakeChild(poll_result=3),
        )
        is None
    )
    assert time.monotonic() - started < 1.0
    assert reads["count"] == 0


def test_daemon_restart_reports_a_child_that_exited_before_a_new_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    child = _FakeChild(poll_result=4)
    terminated: list[Any] = []
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_terminate_child", terminated.append)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    message = json.loads(result.stdout)["error"]["message"]
    assert "exited during startup (exit 4)" in message
    assert terminated == [child]


class _StatusResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeHTTPConnection:
    """Stand-in for ``http.client.HTTPConnection`` (which ignores proxies)."""

    status = 200
    created: list[tuple[str, int]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        type(self).created.append((host, port))

    def request(self, method: str, path: str) -> None:
        return None

    def getresponse(self) -> _StatusResponse:
        return _StatusResponse(type(self).status)

    def close(self) -> None:
        return None


def test_wait_for_ready_returns_true_on_http_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", _FakeHTTPConnection)

    assert daemon_module._wait_for_ready(settings, timeout_seconds=0.1) is True


def test_wait_for_ready_ignores_ambient_http_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    created: list[tuple[str, int]] = []

    class ProxyIgnoringConnection(_FakeHTTPConnection):
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            super().__init__(host, port, timeout)
            created.append((host, port))

    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", ProxyIgnoringConnection)

    assert daemon_module._wait_for_ready(settings, timeout_seconds=0.1) is True
    # The probe went straight to the configured loopback address, not a proxy.
    assert created == [(settings.host, settings.port)]


def test_wait_for_ready_retries_a_bad_status_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    calls = {"count": 0}

    class FlakyConnection(_FakeHTTPConnection):
        def request(self, method: str, path: str) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", FlakyConnection)

    # An HTTPException must be retried, never escape as a traceback.
    assert daemon_module._wait_for_ready(settings, timeout_seconds=1.0) is True
    assert calls["count"] >= 2


def test_wait_for_ready_retries_connection_errors_until_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)

    class RefusedConnection(_FakeHTTPConnection):
        def request(self, method: str, path: str) -> None:
            raise ConnectionRefusedError("not up")

    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", RefusedConnection)

    assert daemon_module._wait_for_ready(settings, timeout_seconds=0.001) is False


def test_wait_for_ready_fails_fast_when_the_child_already_exited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    attempted = {"count": 0}

    class CountingConnection(_FakeHTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            attempted["count"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", CountingConnection)
    started = time.monotonic()

    assert (
        daemon_module._wait_for_ready(
            settings,
            timeout_seconds=30.0,
            child=_FakeChild(poll_result=7),
        )
        is False
    )
    assert time.monotonic() - started < 1.0
    assert attempted["count"] == 0


@pytest.mark.parametrize("raw", ["0", "-1", "1", str(2**32)])
def test_read_pid_rejects_group_and_out_of_range_pids(tmp_path: Path, raw: str) -> None:
    settings = _restart_settings(tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(raw, encoding="utf-8")

    assert daemon_module.read_pid(settings) is None


def test_pid_matches_fails_closed_without_meta(tmp_path: Path) -> None:
    settings = _restart_settings(tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")

    assert daemon_module.pid_matches(settings, os.getpid()) is False


def _fake_ps_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, stdout="Mon Nov 17 10:00:00 2026\n", stderr="")


def test_process_start_time_falls_back_to_ps_when_proc_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_module, "_proc_start_time", lambda _pid: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return _fake_ps_run(args)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)

    assert daemon_module._process_start_time(4321) == "Mon Nov 17 10:00:00 2026"
    args, kwargs = calls[0]
    assert args[1:] == ["-o", "lstart=", "-p", "4321"]
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["timeout"] == daemon_module._PS_TIMEOUT_SECONDS
    assert "shell" not in kwargs


def test_pid_matches_uses_the_ps_fallback_and_stays_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    monkeypatch.setattr(daemon_module, "_proc_start_time", lambda _pid: None)
    monkeypatch.setattr(daemon_module.subprocess, "run", _fake_ps_run)

    write_pid(settings)

    assert daemon_module.pid_matches(settings, os.getpid()) is True


@pytest.mark.parametrize("meta", ["", "{}", "not-json", '{"start_time": null}'])
def test_pid_matches_fails_closed_on_empty_or_corrupt_meta(tmp_path: Path, meta: str) -> None:
    settings = _restart_settings(tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text(meta, encoding="utf-8")

    assert daemon_module.pid_matches(settings, os.getpid()) is False


class _FakeStdin:
    def __init__(self, *, result: bool = False, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def isatty(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._result


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
def test_stdin_is_tty_reports_the_stream(
    monkeypatch: pytest.MonkeyPatch, value: bool, expected: bool
) -> None:
    monkeypatch.setattr(daemon_module.sys, "stdin", _FakeStdin(result=value))

    assert daemon_module._stdin_is_tty() is expected


@pytest.mark.parametrize("error", [AttributeError, ValueError])
def test_stdin_is_tty_false_on_a_broken_stream(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setattr(daemon_module.sys, "stdin", _FakeStdin(error=error("broken")))

    assert daemon_module._stdin_is_tty() is False


def _cleanup_pid_state(settings: Settings) -> None:
    pid_path(settings).unlink(missing_ok=True)
    pid_meta_path(settings).unlink(missing_ok=True)


@pytest.mark.parametrize("meta", ["[]", '"text"', "123", "null"])
def test_read_started_at_returns_none_for_non_dict_meta(tmp_path: Path, meta: str) -> None:
    settings = _restart_settings(tmp_path)
    pid_meta_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_meta_path(settings).write_text(meta, encoding="utf-8")

    assert system_module._read_started_at(settings) is None


def test_run_uvicorn_leaves_log_config_unset_so_root_handlers_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(target: Any, **kwargs: Any) -> None:
        captured["target"] = target
        captured.update(kwargs)

    import uvicorn

    from oft.cli.main import _run_uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    _run_uvicorn(settings)

    # uvicorn must not install its own stderr loggers: the app factory configures
    # the root handlers (stdout + daemon.log), which uvicorn then propagates to.
    assert captured["log_config"] is None
    assert captured["factory"] is True
    assert captured["host"] == settings.host
    assert captured["port"] == settings.port


def test_daemon_restart_build_container_config_error_exits_five(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)

    def raise_config(_settings: Settings, **_kwargs: Any) -> Any:
        raise ConfigError("boom")

    monkeypatch.setattr("oft.bootstrap.build_container", raise_config)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 5, result.output
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_INVALID"


def test_daemon_restart_tty_decline_exits_zero_without_stopping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    before = pid_path(settings).read_text(encoding="utf-8")
    stop_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(daemon_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(daemon_module.typer, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        daemon_module, "_stop_daemon", lambda *args, **kwargs: stop_calls.append(args) or True
    )
    try:
        result = _invoke("daemon", "restart")
        assert pid_path(settings).read_text(encoding="utf-8") == before
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 0, result.output
    assert "restart cancelled" in result.stdout
    assert stop_calls == []
    assert before == str(os.getpid())


def test_daemon_restart_json_tty_decline_emits_a_cancelled_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    stop_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(daemon_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(daemon_module.typer, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        daemon_module, "_stop_daemon", lambda *args, **kwargs: stop_calls.append(args) or True
    )
    try:
        result = _invoke("daemon", "restart", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"] == {"state": "CANCELLED", "pid": os.getpid()}
    assert payload["meta"]["data_quality"] == "COMPLETE"
    assert stop_calls == []


def test_daemon_restart_tty_confirm_true_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    stop_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(daemon_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(daemon_module.typer, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        daemon_module, "_stop_daemon", lambda *args, **kwargs: stop_calls.append(args) or True
    )
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 44444)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)
    try:
        result = _invoke("daemon", "restart", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["state"] == "RESTARTED"
    assert data["pid"] == 44444
    assert len(stop_calls) == 1


def test_daemon_restart_stop_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: False)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "INTERNAL_ERROR"


def test_daemon_restart_spawn_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)

    def raise_os_error(_settings: Settings) -> Any:
        raise OSError("boom")

    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", raise_os_error)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "INTERNAL_ERROR"


def test_daemon_restart_without_a_new_pid_terminates_the_child_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    terminated: list[Any] = []
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "_terminate_child", terminated.append)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "INTERNAL_ERROR"
    assert len(terminated) == 1


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_daemon_restart_treats_a_group_pid_as_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(raw, encoding="utf-8")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "restart", "--yes", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "DAEMON_NOT_RUNNING"
    assert kills == []


def test_daemon_restart_readiness_timeout_terminates_the_child_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    child = _FakeChild(poll_result=None)
    terminated: list[Any] = []
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60001)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module, "_terminate_child", terminated.append)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    message = json.loads(result.stdout)["error"]["message"]
    assert "not ready" in message
    assert terminated == [child]


def test_daemon_restart_reports_a_child_that_exited_during_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    child = _FakeChild(poll_result=1)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60002)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module, "_terminate_child", lambda _child: None)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    message = json.loads(result.stdout)["error"]["message"]
    assert "exited during startup (exit 1)" in message


def test_daemon_restart_failure_reports_a_bounded_redacted_child_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    log = daemon_module._restart_stderr_path(settings)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "startup boom sk-ABCDEFGH12345678 \x1b[31mred\x1b[0m\n" + "x" * 2000,
        encoding="utf-8",
    )
    child = _FakeChild(poll_result=None)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60003)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module, "_terminate_child", lambda _child: None)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    combined = result.stdout + result.stderr
    assert "sk-ABCDEFGH12345678" not in combined
    assert "\x1b" not in combined
    assert len(json.loads(result.stdout)["error"]["message"]) < 700


def test_startup_output_tail_redacts_a_long_secret_before_truncating(tmp_path: Path) -> None:
    secret = "LONG-CONFIGURED-SECRET-" + "A" * 600
    settings = Settings(env="test", data_dir=tmp_path / "data", openai_api_key=secret)
    log = daemon_module._restart_stderr_path(settings)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("startup failed with " + secret + " in the message", encoding="utf-8")

    tail = daemon_module._startup_output_tail(settings)

    # A larger read window means the full secret is redacted before the tail is
    # truncated; no partial prefix/suffix of it can be emitted.
    assert secret not in tail
    assert secret[:80] not in tail
    assert secret[-80:] not in tail
    assert "[REDACTED]" in tail
    assert len(tail) < 700


def test_daemon_restart_failure_fully_redacts_the_startup_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    sentinel = "CONFIGURED-STARTUP-SENTINEL-DO-NOT-LEAK"
    monkeypatch.setenv("OFT_OPENAI_API_KEY", sentinel)
    write_pid(settings)
    log = daemon_module._restart_stderr_path(settings)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "Authorization: Basic dXNlcjpwYXNz\n"
        "Cookie: sid=SID-SENTINEL-DO-NOT-LEAK\n"
        "AKIAABCDEFGHIJKLMNOP\n"
        "bot 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n"
        f"configured {sentinel}\n",
        encoding="utf-8",
    )
    child = _FakeChild(poll_result=None)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60005)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module, "_terminate_child", lambda _child: None)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    combined = result.stdout + result.stderr
    for forbidden in (
        "dXNlcjpwYXNz",
        "SID-SENTINEL-DO-NOT-LEAK",
        "AKIAABCDEFGHIJKLMNOP",
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        sentinel,
    ):
        assert forbidden not in combined


def test_daemon_restart_reports_neutral_text_when_no_positions_are_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60006)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)
    try:
        result = _invoke("daemon", "restart", "--yes")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 0, result.output
    assert "No open positions are affected by this restart." in result.stdout
    assert "warning:" not in result.stdout.lower()


class _PositionStub:
    def __init__(self, position_id: str) -> None:
        self.position_id = position_id


class _StoreSpy:
    def __init__(self) -> None:
        self.count_calls: list[tuple[str | None, str | None]] = []
        self.list_calls: list[tuple[str | None, int | None, int]] = []

    def count_positions(self, *, state: str | None = None, symbol: str | None = None) -> int:
        self.count_calls.append((state, symbol))
        return 150

    def list_positions(
        self,
        *,
        state: str | None = None,
        symbol: str | None = None,
        report_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> tuple[_PositionStub, ...]:
        self.list_calls.append((state, limit, offset))
        return tuple(_PositionStub(f"POS-{index}") for index in range(limit or 0))


class _ContainerSpy:
    def __init__(self, settings: Settings, store: _StoreSpy) -> None:
        from oft.clock import FixedClock

        self.settings = settings
        self.store = store
        self.clock = FixedClock(datetime(2026, 10, 8, 12, 0, tzinfo=UTC))


def test_daemon_restart_counts_all_open_positions_but_reads_only_ten_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    store = _StoreSpy()
    container = _ContainerSpy(settings, store)
    monkeypatch.setattr("oft.bootstrap.build_container", lambda *args, **kwargs: container)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: object())
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60004)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: True)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 0, result.output
    assert store.count_calls == [("OPEN", None)]
    assert store.list_calls == [("OPEN", 10, 0)]
    assert json.loads(result.stdout)["data"]["open_positions"] == 150
    assert "150 open positions remain open" in result.stderr
    assert "POS-0" in result.stderr
    assert "..." in result.stderr
