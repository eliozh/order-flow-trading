"""Integration: ``oft system info|logs`` and ``oft daemon restart`` diagnostics.

These tests exercise the CLI command tree in-process against a temporary data
directory. They cover the offline diagnostics, deterministic log filtering,
recursive redaction, the bounded follow loop, and the restart confirmation and
open-positions contract (F12 / ADR-0003 exit codes 0/1/2/5).
"""

from __future__ import annotations

import http.client
import json
import logging
import logging.config
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
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


def test_run_uvicorn_configures_stderr_logging_that_cannot_reach_root_handlers(
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

    # uvicorn keeps its own stderr handlers with propagation disabled, so its
    # plaintext records never reach the root handlers that write daemon.log.
    config = captured["log_config"]
    assert config is not None
    assert config["handlers"]["default"]["stream"] == "ext://sys.stderr"
    # uvicorn 0.54 indexes the ``access`` formatter when ``use_colors`` is a
    # bool, so the dict must mirror that key even though access_log is off.
    assert "access" in config["formatters"]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["propagate"] is False
    assert captured["access_log"] is False
    assert captured["factory"] is True
    assert captured["host"] == settings.host
    assert captured["port"] == settings.port


def test_uvicorn_log_config_cannot_write_plaintext_lines_to_daemon_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import structlog

    from oft.cli.main import _uvicorn_log_config
    from oft.logging import configure_logging

    settings = _configure_cli(monkeypatch, tmp_path)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_structlog = structlog.get_config()
    uvicorn_logger_names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved_uvicorn_loggers = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in uvicorn_logger_names
    }
    try:
        configure_logging(settings)
        logging.config.dictConfig(_uvicorn_log_config())
        logging.getLogger("uvicorn.error").error(
            "Started server process [123] [Errno 98] address already in use"
        )
        for handler in list(root.handlers):
            handler.flush()
        path = log_path(settings)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        assert "Started server process" not in content
        assert "address already in use" not in content

        # A JSON-only file means a filtered read drops nothing.
        record = _record("2026-10-08T12:00:00.000Z", "info", "uvicorn_config_kept")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        result = _invoke("system", "logs", "--level", "info", "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["data"]["dropped"] == 0
        assert payload["meta"]["data_quality"] == "COMPLETE"
        assert [line["event"] for line in payload["data"]["lines"]] == ["uvicorn_config_kept"]
    finally:
        for name in uvicorn_logger_names:
            logger = logging.getLogger(name)
            saved = saved_uvicorn_loggers[name]
            for handler in list(logger.handlers):
                if handler not in saved[0]:
                    handler.close()
            logger.handlers = saved[0]
            logger.propagate = saved[1]
            logger.setLevel(saved[2])
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        structlog.configure(**saved_structlog)


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


def test_daemon_restart_readiness_timeout_leaves_a_live_child_running_and_exits_one(
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
    assert "pid 60001" in message
    assert "not ready" in message
    # A slow startup catch-up can outlive the readiness budget: the live child
    # published a PID, so it must be left running rather than SIGTERMed.
    assert "not terminated" in message
    assert "may still be starting" in message
    assert terminated == []


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


# ---------------------------------------------------------------------------
# Fix round 1 regressions
# ---------------------------------------------------------------------------


def test_daemon_stop_clears_a_positively_mismatched_pid_without_signalling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text(
        '{"pid": 1, "start_time": "definitely-not-the-recorded-start-time"}',
        encoding="utf-8",
    )
    # ``process_alive`` probes with ``os.kill(pid, 0)``; stub it true so the
    # assertion below proves no signal of any kind was sent.
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()
    assert kills == []
    assert "not signalled" in result.stderr
    assert "may still be running" in result.stderr


def test_daemon_stop_clears_a_live_unrelated_pid_with_legacy_null_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text('{"pid": 1, "start_time": null}', encoding="utf-8")
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: False)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()
    assert kills == []
    assert "not signalled" in result.stderr


def test_reconcile_pid_fence_adopts_a_legacy_null_fence_for_an_oft_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text(
        json.dumps(
            {
                "pid": pid,
                "start_time": None,
                "started_at": "2026-01-01T00:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_same_uid", lambda _pid: True)

    assert daemon_module._reconcile_pid_fence(settings, pid) == "adopted"
    meta = json.loads(pid_meta_path(settings).read_text(encoding="utf-8"))
    assert meta["start_time"] == "current-start-time"
    assert meta["pid"] == pid
    assert meta["started_at"] == "2026-01-01T00:00:00.000Z"
    assert (pid_meta_path(settings).stat().st_mode & 0o777) == 0o600


def test_daemon_stop_adopts_and_stops_a_legacy_null_fence_oft_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text(
        json.dumps({"pid": pid, "start_time": None}), encoding="utf-8"
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_same_uid", lambda _pid: True)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "STOPPED", "pid": pid}
    assert kills == [(pid, daemon_module.signal.SIGTERM)]
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_daemon_stop_keeps_artifacts_when_a_live_oft_pid_fence_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: None)
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert pid_path(settings).exists()
    assert kills == []
    assert "pid artifacts kept" in result.stderr
    assert "retry" in result.stderr.lower()


def test_daemon_stop_clears_a_live_pid_that_is_not_an_oft_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: False)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"state": "NOT_RUNNING"}
    assert not pid_path(settings).exists()
    assert kills == []
    assert "may still be running" in result.stderr


def test_daemon_restart_clears_a_stale_unverifiable_pid_and_reports_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text(
        '{"pid": 1, "start_time": "definitely-not-the-recorded-start-time"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "restart", "--yes", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "DAEMON_NOT_RUNNING"
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()
    assert kills == []


def test_daemon_restart_reports_retry_when_a_live_oft_pid_fence_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: None)
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    result = _invoke("daemon", "restart", "--yes", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "DAEMON_NOT_RUNNING"
    assert "retry" in payload["error"]["message"].lower()
    assert pid_path(settings).exists()
    assert kills == []


def test_daemon_start_refuses_while_a_stale_pid_artifact_exists_then_stop_clears_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text(
        '{"pid": 1, "start_time": "definitely-not-the-recorded-start-time"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    started = _invoke("daemon", "start")

    assert started.exit_code == 1, started.output
    assert "already running" in started.stderr
    # The refusal must point at the recovery command.
    assert "oft daemon stop" in started.stderr
    assert pid_path(settings).exists()
    assert pid_meta_path(settings).exists()
    assert kills == []

    stopped = _invoke("daemon", "stop", "--json")

    assert stopped.exit_code == 0, stopped.output
    assert daemon_module.read_pid(settings) is None
    assert not pid_meta_path(settings).exists()
    assert kills == []


def test_system_logs_rejects_a_huge_relative_since_as_a_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    result = _invoke("system", "logs", "--since", "99999999999999999999d", "--json")

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "VALIDATION_FAILED"
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert "OverflowError" not in combined


def test_parse_since_enforces_the_relative_window_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    fixed = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(system_module, "_now", lambda _settings: fixed)
    boundary_days = system_module._MAX_RELATIVE_SINCE_SECONDS // 86400

    assert fixed - system_module._parse_since(f"{boundary_days}d", settings) == timedelta(
        days=boundary_days
    )
    with pytest.raises(ValueError):
        system_module._parse_since(f"{boundary_days + 1}d", settings)


def test_daemon_restart_reports_a_uvicorn_bind_error_from_the_stderr_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    write_pid(settings)
    stderr = daemon_module._restart_stderr_path(settings)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    stderr.write_text(
        "[Errno 98] error while attempting to bind on address "
        "('127.0.0.1', 7611): address already in use\n",
        encoding="utf-8",
    )
    child = _FakeChild(poll_result=None)
    monkeypatch.setattr(daemon_module, "_stop_daemon", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module, "_spawn_detached_daemon", lambda _settings: child)
    monkeypatch.setattr(daemon_module, "_wait_for_new_pid", lambda *args, **kwargs: 60007)
    monkeypatch.setattr(daemon_module, "_wait_for_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module, "_terminate_child", lambda _child: None)
    try:
        result = _invoke("daemon", "restart", "--yes", "--json")
    finally:
        _cleanup_pid_state(settings)

    assert result.exit_code == 1, result.output
    message = json.loads(result.stdout)["error"]["message"]
    assert "address already in use" in message
    assert "restart-stderr.log" in message
    assert len(message) < 700


@pytest.mark.parametrize("stderr_state", ["absent", "empty"])
def test_startup_output_tail_falls_back_to_daemon_log_when_stderr_is_unusable(
    tmp_path: Path, stderr_state: str
) -> None:
    settings = _restart_settings(tmp_path)
    stderr = daemon_module._restart_stderr_path(settings)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    if stderr_state == "empty":
        stderr.write_text("", encoding="utf-8")
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    # daemon.log is JSON lines; uvicorn's bind failure reaches it as a
    # structured record (or not at all), never as a bare plaintext line.
    daemon_log.write_text(
        json.dumps(
            {
                "event": "startup_failed",
                "message": (
                    "[Errno 98] error while attempting to bind on address: address already in use"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    tail = daemon_module._startup_output_tail(settings)

    assert "address already in use" in tail
    assert "daemon.log" in tail
    assert "restart-stderr.log" not in tail


def test_select_records_treats_recursion_depth_json_as_unparseable() -> None:
    # Deep enough that the JSON decoder itself raises ``RecursionError``; the
    # exact threshold depends on the interpreter's C recursion limit.
    nested = "[" * 20000 + "]" * 20000

    unfiltered = system_module._select_records([nested], since=None, min_level=None)
    assert unfiltered.matched == 1
    assert unfiltered.dropped == 0
    assert unfiltered.records == (nested,)

    filtered = system_module._select_records([nested], since=None, min_level=20)
    assert filtered.matched == 0
    assert filtered.dropped == 1
    assert filtered.records == ()


def test_system_logs_drops_a_recursion_depth_json_line_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[" * 20000
        + "]" * 20000
        + "\n"
        + json.dumps(_record("2026-10-08T12:00:00.000Z", "info", "kept"))
        + "\n",
        encoding="utf-8",
    )

    result = _invoke("system", "logs", "--level", "info", "--json")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "RecursionError" not in result.output
    data = json.loads(result.stdout)["data"]
    assert data["dropped"] == 1
    assert [line["event"] for line in data["lines"]] == ["kept"]


def test_system_logs_follow_drops_a_recursion_depth_json_line_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_record("2026-10-08T12:00:00.000Z", "info", "initial_event")) + "\n",
        encoding="utf-8",
    )
    nested = "[" * 20000 + "]" * 20000
    appended = _record("2026-10-08T12:01:00.000Z", "info", "appended_event")
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(nested + "\n")
            return
        if calls["count"] == 2:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(appended) + "\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--level", "info")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "RecursionError" not in result.output
    assert "initial_event" in result.stdout
    assert "appended_event" in result.stdout


def test_system_logs_follow_withholds_a_partial_initial_record_until_it_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The final record is torn mid-value: the first read must not emit it.
    path.write_text(
        '{"timestamp":"2026-10-08T12:00:00.000Z","level":"info",'
        '"event":"split_event","api_key":"tok-SPLIT-',
        encoding="utf-8",
    )
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write('SENTINEL"}\n')
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--json")

    assert result.exit_code == 0, result.output
    # The fragment is never emitted raw (its api_key is not redacted yet) and
    # the completed record is emitted exactly once, fully redacted.
    assert "tok-SPLIT-" not in result.stdout
    assert "SENTINEL" not in result.stdout
    documents = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [document["event"] for document in documents] == ["split_event"]
    assert documents[0]["api_key"] == system_module.REDACTED


def test_wait_for_ready_treats_a_child_without_poll_as_still_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)

    class RefusedConnection(_FakeHTTPConnection):
        def request(self, method: str, path: str) -> None:
            raise ConnectionRefusedError("not up")

    monkeypatch.setattr(daemon_module.http.client, "HTTPConnection", RefusedConnection)

    # An injected child without ``poll`` is the documented test seam: it must
    # be treated as running rather than raising ``AttributeError``.
    assert daemon_module._wait_for_ready(settings, timeout_seconds=0.001, child=object()) is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("credentials", "user:hunter2-plain"),
        ("bearer", "opaque-token-xyz"),
        ("private_key", "opaque-private-material"),
        ("passphrase", "correct horse battery staple"),
        ("pwd", "hunter2"),
        ("passwd", "PLAINTEXT-HUNTER2"),
        ("access_key", "opaque-access-key"),
    ],
)
def test_redact_record_redacts_credential_field_names(key: str, value: str) -> None:
    redacted = system_module.redact_record({key: value})

    assert redacted == {key: system_module.REDACTED}


def test_redact_record_does_not_redact_author_keys() -> None:
    record = {"author": "Ada", "author_name": "Ada Lovelace"}

    assert system_module.redact_record(record) == record


def test_redact_record_still_keeps_prompt_metadata_after_new_hints() -> None:
    record = {"prompt_id": "analysis", "prompt_version": 9}

    assert system_module.redact_record(record) == record


@pytest.mark.parametrize(
    "text",
    [
        "Authorization=Basic dXNlcjpwYXNz",
        'Authorization=Digest username="x", response="secret-response-hash"',
        "Proxy-Authorization=Basic dXNlcjpwYXNz",
        "proxy-authorization: Basic dXNlcjpwYXNz",
    ],
)
def test_redact_record_redacts_authorization_assignments(text: str) -> None:
    redacted = system_module.redact_record({"message": text})

    assert "dXNlcjpwYXNz" not in redacted["message"]
    assert "secret-response-hash" not in redacted["message"]
    assert "Authorization" in redacted["message"]


def test_redact_record_leaves_author_prose_alone() -> None:
    message = "the author wrote this sentence"

    assert system_module.redact_record({"message": message}) == {"message": message}


# ---------------------------------------------------------------------------
# Fix round 1b regressions
# ---------------------------------------------------------------------------


def _nested_array(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_system_logs_marker_for_a_depth_the_decoder_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_nested_array(2000) + "\n", encoding="utf-8")

    result = _invoke("system", "logs", "--json")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "RecursionError" not in result.output
    assert system_module._UNPARSEABLE_RECORD in result.stdout
    # Never emit the raw structured text that bypassed redaction.
    assert "[[[[" not in result.stdout


def test_system_logs_drops_a_record_whose_nested_field_breaks_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        '{"timestamp":"2026-10-08T12:00:00.000Z","level":"info",'
        '"event":"deep","payload":' + _nested_array(2000) + "}"
    )
    path.write_text(line + "\n", encoding="utf-8")

    result = _invoke("system", "logs", "--level", "info", "--json")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "RecursionError" not in result.output
    data = json.loads(result.stdout)["data"]
    assert data["dropped"] == 1
    assert data["lines"] == []


def test_system_logs_follow_uses_the_marker_for_a_depth_the_decoder_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_nested_array(2000) + "\n", encoding="utf-8")
    appended = _record("2026-10-08T12:01:00.000Z", "info", "appended_event")
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(appended) + "\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--json")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "RecursionError" not in result.output
    assert system_module._UNPARSEABLE_RECORD in result.stdout
    assert "appended_event" in result.stdout


@pytest.mark.parametrize("stderr_state", ["absent", "empty"])
def test_startup_output_tail_redacts_structured_daemon_log_records(
    tmp_path: Path, stderr_state: str
) -> None:
    settings = _restart_settings(tmp_path)
    stderr = daemon_module._restart_stderr_path(settings)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    if stderr_state == "empty":
        stderr.write_text("", encoding="utf-8")
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_log.write_text(
        json.dumps(
            {
                "credentials": "user:hunter2-plain",
                "bearer": "opaque-token-xyz",
                "headers": {"authorization": "Basic dXNlcjpwYXNz"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    tail = daemon_module._startup_output_tail(settings)

    assert "hunter2-plain" not in tail
    assert "opaque-token-xyz" not in tail
    assert "dXNlcjpwYXNz" not in tail
    assert "[REDACTED]" in tail
    assert "daemon.log" in tail


def test_startup_output_tail_names_daemon_log_when_both_captures_are_empty(
    tmp_path: Path,
) -> None:
    settings = _restart_settings(tmp_path)
    stderr = daemon_module._restart_stderr_path(settings)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    stderr.write_text("", encoding="utf-8")
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_log.write_text("", encoding="utf-8")

    tail = daemon_module._startup_output_tail(settings)

    assert "no child output" in tail
    assert "daemon.log" in tail
    assert "restart-stderr.log" not in tail


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("credentials=user:hunter2-plain", "hunter2-plain"),
        ("credential=user:hunter2-plain", "hunter2-plain"),
        ("bearer=opaque-token-xyz", "opaque-token-xyz"),
        ("passphrase=correct-horse-battery", "correct-horse-battery"),
        ("private_key=opaque-private-material", "opaque-private-material"),
        ("private-key=opaque-private-material", "opaque-private-material"),
        ("pwd=hunter2", "hunter2"),
        ("access_key=opaque-access-key", "opaque-access-key"),
        ("access-key=opaque-access-key", "opaque-access-key"),
    ],
)
def test_redact_record_redacts_free_text_credential_assignments(text: str, forbidden: str) -> None:
    redacted = system_module.redact_record({"message": text})

    assert forbidden not in redacted["message"]
    assert system_module.REDACTED in redacted["message"]


@pytest.mark.parametrize(
    "text",
    [
        "{'authorization': 'Basic dXNlcjpwYXNz'}",
        '{"authorization": "Basic dXNlcjpwYXNz"}',
        "proxy_authorization='Basic dXNlcjpwYXNz'",
        "proxy-authorization=Basic dXNlcjpwYXNz",
    ],
)
def test_redact_record_redacts_quoted_and_proxy_authorization_forms(text: str) -> None:
    redacted = system_module.redact_record({"message": text})

    assert "dXNlcjpwYXNz" not in redacted["message"]
    assert "Authorization" in redacted["message"]


# ---------------------------------------------------------------------------
# Fix round 1c regressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ('{"credentials":"user:hunter2-plain"}', "hunter2-plain"),
        ('{"password":"hunter2-real"}', "hunter2-real"),
        ('{"api_key":"ak-SENTINEL"}', "ak-SENTINEL"),
    ],
)
def test_redact_record_redacts_quoted_key_assignments(text: str, forbidden: str) -> None:
    redacted = system_module.redact_record({"message": text})

    assert forbidden not in redacted["message"]
    assert system_module.REDACTED in redacted["message"]


def test_redact_record_leaves_a_quoted_author_assignment_alone() -> None:
    text = '{"author":"Ada","author_name":"Ada Lovelace"}'

    assert system_module.redact_record({"message": text}) == {"message": text}


def test_system_logs_redacts_quoted_credentials_in_a_retained_text_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not valid JSON (leading prose), so the line is retained as redacted text.
    path.write_text(
        'garbage {"credentials":"user:hunter2-plain"}\n',
        encoding="utf-8",
    )

    result = _invoke("system", "logs", "--json")

    assert result.exit_code == 0, result.output
    assert "hunter2-plain" not in result.stdout
    lines = json.loads(result.stdout)["data"]["lines"]
    assert len(lines) == 1
    assert system_module.REDACTED in lines[0]


def test_system_logs_follow_redacts_quoted_credentials_in_a_retained_text_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'garbage {"credentials":"user:hunter2-plain"}\n',
        encoding="utf-8",
    )
    appended = _record("2026-10-08T12:01:00.000Z", "info", "appended_event")
    calls = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(appended) + "\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(system_module, "_sleep", fake_sleep)

    result = _invoke("system", "logs", "--follow", "--json")

    assert result.exit_code == 0, result.output
    assert "hunter2-plain" not in result.stdout
    assert "appended_event" in result.stdout


def test_startup_output_tail_withholds_a_torn_daemon_log_record(tmp_path: Path) -> None:
    settings = _restart_settings(tmp_path)
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_log.write_text(
        '{"event":"scan","credentials":"opaque-cred","payload":{"x":',
        encoding="utf-8",
    )

    tail = daemon_module._startup_output_tail(settings)

    assert "opaque-cred" not in tail
    assert '{"event"' not in tail
    assert "incomplete" in tail
    assert "daemon.log" in tail


def test_startup_output_tail_markers_a_malformed_daemon_log_line(tmp_path: Path) -> None:
    settings = _restart_settings(tmp_path)
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_log.write_text('{"password":"hunter2-real"\n', encoding="utf-8")

    tail = daemon_module._startup_output_tail(settings)

    assert "hunter2-real" not in tail
    assert daemon_module._UNPARSEABLE_OUTPUT_LINE in tail
    assert "daemon.log" in tail


def test_startup_output_tail_withholds_a_torn_oversized_daemon_log_line(
    tmp_path: Path,
) -> None:
    settings = _restart_settings(tmp_path)
    daemon_log = log_path(settings)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_log.write_text(
        "x" * 40000 + ',"credentials":"opaque-cred"}',
        encoding="utf-8",
    )

    tail = daemon_module._startup_output_tail(settings)

    assert "opaque-cred" not in tail
    assert "incomplete" in tail
    assert "daemon.log" in tail


def test_startup_output_tail_markers_a_json_looking_stderr_partial_line(
    tmp_path: Path,
) -> None:
    settings = _restart_settings(tmp_path)
    stderr = daemon_module._restart_stderr_path(settings)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    stderr.write_text(
        '{"event":"scan","credentials":"opaque-cred","payload":{"x":',
        encoding="utf-8",
    )

    tail = daemon_module._startup_output_tail(settings)

    assert "opaque-cred" not in tail
    assert daemon_module._UNPARSEABLE_OUTPUT_LINE in tail


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m oft.cli.main daemon start", True),
        ("/home/me/bin/oft daemon start", True),
        ("vim /home/me/oft/README.md", False),
        ("grep daemon start notes.txt", False),
        ("oft status", False),
    ],
)
def test_looks_like_oft_daemon_uses_a_strict_identity(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: bool
) -> None:
    monkeypatch.setattr(daemon_module, "_process_command", lambda _pid: command)

    assert daemon_module._looks_like_oft_daemon(123) is expected


def test_adopt_pid_start_time_returns_false_on_write_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    pid_meta_path(settings).parent.mkdir(parents=True, exist_ok=True)

    def raise_oserror(self: Path, *args: Any, **kwargs: Any) -> int:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", raise_oserror)

    assert daemon_module._adopt_pid_start_time(settings, os.getpid(), "current") is False


def test_reconcile_pid_fence_keeps_artifacts_when_adoption_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text(
        json.dumps({"pid": pid, "start_time": None}), encoding="utf-8"
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_same_uid", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_adopt_pid_start_time", lambda *args: False)

    assert daemon_module._reconcile_pid_fence(settings, pid) == "kept"
    assert pid_path(settings).exists()


def test_reconcile_pid_fence_keeps_artifacts_when_the_current_start_time_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text(
        json.dumps({"pid": pid, "start_time": "recorded-start-time"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: None)
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: False)

    assert daemon_module._reconcile_pid_fence(settings, pid) == "kept"
    assert pid_path(settings).exists()
    assert pid_meta_path(settings).exists()


def test_reconcile_pid_fence_does_not_adopt_a_foreign_uid_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    pid = os.getpid()
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(pid), encoding="utf-8")
    pid_meta_path(settings).write_text(
        json.dumps({"pid": pid, "start_time": None}), encoding="utf-8"
    )
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_same_uid", lambda _pid: False)

    assert daemon_module._reconcile_pid_fence(settings, pid) == "cleared"
    assert not pid_path(settings).exists()
    assert not pid_meta_path(settings).exists()


def test_deeply_nested_pid_meta_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    pid_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pid_path(settings).write_text(str(os.getpid()), encoding="utf-8")
    pid_meta_path(settings).write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
    monkeypatch.setattr(daemon_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_module, "_process_start_time", lambda _pid: "current-start-time")
    monkeypatch.setattr(daemon_module, "_looks_like_oft_daemon", lambda _pid: False)

    assert daemon_module._read_pid_meta(settings) is None
    assert daemon_module.pid_matches(settings, os.getpid()) is False

    status = _invoke("daemon", "status", "--json")
    assert status.exit_code == 0, status.output
    stopped = _invoke("daemon", "stop", "--json")
    assert stopped.exit_code == 0, stopped.output
    assert "Traceback" not in status.output + stopped.output


def test_stop_daemon_permission_denied_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _restart_settings(tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)

    def raise_permission(_pid: int, _signal: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(daemon_module.os, "kill", raise_permission)

    assert daemon_module._stop_daemon(settings, 12345, timeout_seconds=0.001) is False
    assert pid_path(settings).exists()
    assert pid_meta_path(settings).exists()


def test_daemon_stop_permission_denied_exits_one_with_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_cli(monkeypatch, tmp_path)
    _seed_pid_files(settings)
    monkeypatch.setattr(daemon_module, "pid_matches", lambda *_args, **_kwargs: True)

    def raise_permission(_pid: int, _signal: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(daemon_module.os, "kill", raise_permission)

    result = _invoke("daemon", "stop", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert "permission denied" in payload["error"]["message"]
    assert "12345" in payload["error"]["message"]
    assert "Traceback" not in result.output
