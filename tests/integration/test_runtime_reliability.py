"""Readiness depth, scheduler heartbeat, and daemon identity probing (slice 56)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import oft
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application import health as health_module
from oft.application import scheduler as scheduler_module
from oft.application.health import build_health, build_readiness
from oft.application.scheduler import (
    SCHEDULER_HEARTBEAT_STALE_SECONDS,
    read_scheduler_heartbeat,
    scheduler_heartbeat_path,
    write_scheduler_heartbeat,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import SchemaState

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path, *, env: str = "test") -> Settings:
    settings = Settings(env=env, port=8765, data_dir=tmp_path / "data")
    SqliteStore(settings).initialize()
    ParquetStore(settings).ensure_layout()
    return settings


def _readiness(settings: Settings, *, now: datetime = NOW) -> Any:
    return build_readiness(
        settings,
        SqliteStore(settings),
        FixtureMarketData(),
        FixedClock(now),
    )


def _check(report: Any, name: str) -> Any:
    return next(check for check in report.checks if check.name == name)


def test_readiness_is_not_ready_when_the_data_disk_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    usage = SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=0)
    monkeypatch.setattr(health_module.shutil, "disk_usage", lambda _path: usage)

    report = _readiness(settings)

    assert report.status == "not_ready"
    assert _check(report, "storage.disk").status == "degraded"


def test_readiness_is_not_ready_when_the_schema_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        SqliteStore,
        "schema_state",
        lambda _self: SchemaState(version=29, expected=30, up_to_date=False),
    )

    report = _readiness(settings)

    assert report.status == "not_ready"
    check = _check(report, "storage.schema")
    assert check.status == "degraded"
    assert "29" in check.detail and "30" in check.detail


def test_readiness_is_not_ready_when_the_scheduler_heartbeat_is_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="paper")

    report = _readiness(settings)

    assert report.status == "not_ready"
    check = _check(report, "scheduler.heartbeat")
    assert check.status == "degraded"
    assert "missing" in check.detail


def test_readiness_is_ok_when_the_scheduler_heartbeat_is_fresh(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="paper")
    write_scheduler_heartbeat(settings, now=NOW)

    report = _readiness(settings, now=NOW + timedelta(seconds=5))

    assert report.status == "ready"
    assert _check(report, "scheduler.heartbeat").status == "ok"


def test_readiness_is_not_ready_when_the_scheduler_heartbeat_is_stale(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="paper")
    write_scheduler_heartbeat(settings, now=NOW)

    report = _readiness(
        settings, now=NOW + timedelta(seconds=SCHEDULER_HEARTBEAT_STALE_SECONDS + 1)
    )

    assert report.status == "not_ready"
    assert _check(report, "scheduler.heartbeat").status == "degraded"


def test_test_env_does_not_require_a_scheduler_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="test")
    assert not scheduler_heartbeat_path(settings).exists()

    report = _readiness(settings)

    assert report.status == "ready"
    check = _check(report, "scheduler.heartbeat")
    assert check.status == "ok"
    assert "not required in test env" in check.detail


def test_schema_state_reports_a_missing_database_as_not_up_to_date(tmp_path: Path) -> None:
    settings = Settings(env="paper", port=8765, data_dir=tmp_path / "data")

    state = SqliteStore(settings).schema_state()

    assert state.up_to_date is False
    assert state.version == 0
    assert state.expected > 0


def test_scheduler_heartbeat_write_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, env="paper")

    def fail_replace(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("no space left")

    monkeypatch.setattr(scheduler_module.os, "replace", fail_replace)

    write_scheduler_heartbeat(settings, now=NOW)

    assert read_scheduler_heartbeat(settings) is None


def test_health_payload_identifies_the_service() -> None:
    assert build_health().service == "oft"


def _envelope(*, service: str = "oft", version: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {"service": service, "version": version or oft.__version__},
    }


def test_probe_daemon_is_up_for_a_matching_service_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(health_module, "_fetch_health", lambda _settings, _timeout: _envelope())

    probe = health_module.probe_daemon(settings)

    assert probe.state == "UP"
    assert "oft" in probe.detail


def test_probe_daemon_is_offline_for_a_squatted_service_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(
        health_module,
        "_fetch_health",
        lambda _settings, _timeout: _envelope(service="someone-else"),
    )

    probe = health_module.probe_daemon(settings)

    assert probe.state == "OFFLINE"
    assert "someone-else" not in probe.detail


def test_probe_daemon_is_offline_for_a_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(
        health_module,
        "_fetch_health",
        lambda _settings, _timeout: _envelope(version="0.0.0-other"),
    )

    probe = health_module.probe_daemon(settings)

    assert probe.state == "OFFLINE"
    assert "0.0.0-other" not in probe.detail


class _RefusingConnection:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise OSError("connection refused")


def test_probe_daemon_is_offline_when_the_port_refuses_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(health_module.http.client, "HTTPConnection", _RefusingConnection)

    probe = health_module.probe_daemon(settings)

    assert probe.state == "OFFLINE"


class _MalformedResponse:
    status = 200

    def read(self, _limit: int = -1) -> bytes:
        return b"<html>definitely not an OFT envelope</html>"


class _MalformedConnection:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getresponse(self) -> _MalformedResponse:
        return _MalformedResponse()

    def close(self) -> None:
        return None


def test_probe_daemon_is_offline_for_malformed_json_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(health_module.http.client, "HTTPConnection", _MalformedConnection)

    probe = health_module.probe_daemon(settings)

    assert probe.state == "OFFLINE"
    assert "definitely not" not in probe.detail


class _DeeplyNestedResponse:
    status = 200

    def read(self, _limit: int = -1) -> bytes:
        # Valid JSON nested deeply enough to exhaust the parser's recursion
        # limit; well under the 64 KiB body cap.
        return b"[" * 10_000 + b"]" * 10_000


class _DeeplyNestedConnection:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getresponse(self) -> _DeeplyNestedResponse:
        return _DeeplyNestedResponse()

    def close(self) -> None:
        return None


def test_probe_daemon_is_offline_for_deeply_nested_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(health_module.http.client, "HTTPConnection", _DeeplyNestedConnection)

    probe = health_module.probe_daemon(settings)

    assert probe.state == "OFFLINE"
    # A fixed detail string: the hostile body is never echoed.
    assert probe.detail == f"no OFT health endpoint on {settings.host}:{settings.port}"


class _HealthyResponse:
    status = 200

    def __init__(self) -> None:
        self._body = json.dumps(_envelope()).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class _HealthyConnection:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getresponse(self) -> _HealthyResponse:
        return _HealthyResponse()

    def close(self) -> None:
        return None


def test_probe_daemon_parses_a_real_health_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    monkeypatch.setattr(health_module.http.client, "HTTPConnection", _HealthyConnection)

    probe = health_module.probe_daemon(settings)

    assert probe.state == "UP"
    assert "healthy" in probe.detail


def test_probe_daemon_default_timeout_covers_a_real_http_round_trip() -> None:
    import inspect

    default = inspect.signature(health_module.probe_daemon).parameters["timeout"].default
    assert default >= 1.0


def test_readiness_error_codes_cover_the_new_checks() -> None:
    from oft.api.routes.system import readiness_error_code
    from oft.application.health import CheckResult

    assert (
        readiness_error_code((CheckResult("storage.disk", "degraded", "x"),))
        == "STORAGE_UNAVAILABLE"
    )
    assert (
        readiness_error_code((CheckResult("storage.schema", "degraded", "x"),))
        == "STORAGE_UNAVAILABLE"
    )
    assert (
        readiness_error_code((CheckResult("scheduler.heartbeat", "degraded", "x"),))
        == "DAEMON_NOT_RUNNING"
    )


def test_readiness_is_not_ready_for_a_naive_scheduler_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="paper")
    scheduler_heartbeat_path(settings).parent.mkdir(parents=True, exist_ok=True)
    scheduler_heartbeat_path(settings).write_text(
        json.dumps({"updated_at": "2026-11-19T12:00:00"}), encoding="utf-8"
    )

    report = _readiness(settings)

    assert report.status == "not_ready"
    check = _check(report, "scheduler.heartbeat")
    assert check.status == "degraded"
    assert "timezone-aware" in check.detail


def test_readiness_is_not_ready_for_a_future_scheduler_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="paper")
    write_scheduler_heartbeat(settings, now=NOW + timedelta(seconds=120))

    report = _readiness(settings)

    assert report.status == "not_ready"
    check = _check(report, "scheduler.heartbeat")
    assert check.status == "degraded"
    assert "future" in check.detail


def test_schema_state_treats_a_newer_database_as_not_up_to_date(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    expected = store.schema_state().expected
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (expected + 1, "2026-11-19T00:00:00.000Z"),
        )

    state = store.schema_state()

    assert state.version == expected + 1
    assert state.up_to_date is False


def test_schema_state_escapes_a_data_dir_containing_a_question_mark(tmp_path: Path) -> None:
    settings = Settings(env="paper", port=8765, data_dir=tmp_path / "data?dir")
    SqliteStore(settings).initialize()

    state = SqliteStore(settings).schema_state()

    assert state.up_to_date is True


def test_heartbeat_temp_name_is_unique_per_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(env="paper", data_dir=tmp_path / "data")
    captured: dict[str, str] = {}

    def capture_replace(source: Path, destination: Path) -> None:
        captured["source"] = str(source)

    monkeypatch.setattr(scheduler_module.os, "replace", capture_replace)

    write_scheduler_heartbeat(settings, now=NOW)

    assert str(threading.get_ident()) in captured["source"]
