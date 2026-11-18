"""Degraded health: Telegram state reaches /system, doctor, and system info."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.application.health import DaemonProbe, build_doctor
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    POLLER_HEARTBEAT_KEY,
    POLLER_LAST_ERROR_KEY,
    POLLER_STATE_CONFLICT,
    POLLER_STATE_DEGRADED,
    POLLER_STATE_KEY,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    TELEGRAM_CHAT_IDS_KEY,
    TELEGRAM_OUTBOX_OVERFLOW_KEY,
    TELEGRAM_QUEUE_FULL,
)
from oft.ports.store import ConfigRecord, TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
RAW_BODY_SENTINEL = "RAW-BOT-API-BODY-DO-NOT-LEAK"


@pytest.fixture(autouse=True)
def _hermetic_secret_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the fail keyring + a per-test master key so CI stays hermetic."""
    import keyring
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("OFT_MASTER_KEY", "per-test-master-key-0123456789abcdef")
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def _settings(tmp_path: Path, *, token: str | None = SENTINEL) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        telegram_bot_token=token,
    )


def _seed(tmp_path: Path) -> tuple[Settings, SqliteStore]:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)
    store.initialize()
    ParquetStore(settings).ensure_layout()
    store.set_config(
        ConfigRecord(
            key=TELEGRAM_CHAT_IDS_KEY,
            value_json=json.dumps([1001, 2002]),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )
    store.set_config(
        ConfigRecord(
            key=TELEGRAM_ALLOWED_USER_IDS_KEY,
            value_json=json.dumps([2002]),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )
    store.set_system_state(POLLER_HEARTBEAT_KEY, NOW_TEXT, updated_at=NOW_TEXT)
    store.set_system_state(POLLER_STATE_KEY, "RUNNING", updated_at=NOW_TEXT)

    def receipt(
        event_key: str,
        chat_id: int,
        state: str,
        *,
        message_id: int | None = None,
        error: str | None = None,
        attempts: int = 0,
        sent_at: str | None = None,
    ) -> TelegramOutboxRecord:
        return TelegramOutboxRecord(
            event_key=event_key,
            kind="scan_report",
            chat_id=chat_id,
            reply_to_message_id=None,
            payload_json=json.dumps({"text": "never rendered in diagnostics"}),
            state=state,  # type: ignore[arg-type]
            attempts=attempts,
            next_attempt_at=NOW_TEXT,
            message_id=message_id,
            last_error_code=error,
            created_at=NOW_TEXT,
            sent_at=sent_at,
        )

    store.append_telegram_outbox(
        receipt(
            "failed-1",
            1001,
            "failed",
            error="TELEGRAM_UPSTREAM_ERROR",
            attempts=3,
        )
    )
    store.append_telegram_outbox(receipt("sent-1", 2002, "sent", message_id=900, sent_at=NOW_TEXT))
    store.append_telegram_outbox(receipt("queued-1", 2002, "queued"))
    return settings, store


def _checks(settings: Settings, store: SqliteStore) -> dict[str, Any]:
    report = build_doctor(
        settings,
        store,
        FixtureMarketData(fixtures_dir=settings.fixtures_dir),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    return {check.name: check for check in report.checks}, report


def test_failing_receipts_degrade_doctor_and_expose_per_chat_status(tmp_path: Path) -> None:
    settings, store = _seed(tmp_path)

    checks, report = _checks(settings, store)

    assert checks["telegram"].status == "degraded"
    assert "failing receipt" in checks["telegram"].detail
    telegram = report.telegram
    assert telegram is not None
    assert telegram["configured"] is True
    assert telegram["chat_count"] == 2
    assert telegram["allowed_user_count"] == 1
    assert telegram["poller_state"] == "RUNNING"
    assert telegram["last_poll_at"] == NOW_TEXT
    assert telegram["last_send_at"] == NOW_TEXT
    assert telegram["failing_receipts"] == 1
    by_chat = {entry["chat_id"]: entry for entry in telegram["per_chat"]}
    # One chat's failure never marks another chat failed.
    assert by_chat[1001]["failed"] == 1
    assert by_chat[1001]["last_error_code"] == "TELEGRAM_UPSTREAM_ERROR"
    assert by_chat[2002]["failed"] == 0
    assert by_chat[2002]["sent"] == 1


def test_a_conflict_poller_state_degrades_doctor(tmp_path: Path) -> None:
    settings, store = _seed(tmp_path)
    store.set_system_state(POLLER_STATE_KEY, POLLER_STATE_CONFLICT, updated_at=NOW_TEXT)
    store.set_system_state(POLLER_LAST_ERROR_KEY, "TELEGRAM_CONFLICT", updated_at=NOW_TEXT)

    checks, report = _checks(settings, store)

    assert checks["telegram"].status == "degraded"
    # The enum state is humanized; the raw port code stays the reason.
    assert "another poller owns this token" in checks["telegram"].detail
    assert "TELEGRAM_CONFLICT" in checks["telegram"].detail
    assert report.telegram is not None
    assert report.telegram["poller_state"] == POLLER_STATE_CONFLICT


def test_a_token_invalid_poller_degrades_doctor_with_the_reason(tmp_path: Path) -> None:
    settings, store = _seed(tmp_path)
    store.set_system_state(POLLER_STATE_KEY, POLLER_STATE_DEGRADED, updated_at=NOW_TEXT)
    store.set_system_state(POLLER_LAST_ERROR_KEY, "TELEGRAM_TOKEN_INVALID", updated_at=NOW_TEXT)

    checks, report = _checks(settings, store)

    assert checks["telegram"].status == "degraded"
    assert "poller DEGRADED" in checks["telegram"].detail
    assert "TELEGRAM_TOKEN_INVALID" in checks["telegram"].detail
    assert report.telegram is not None
    assert report.telegram["poller_state"] == POLLER_STATE_DEGRADED
    assert report.telegram["last_error_code"] == "TELEGRAM_TOKEN_INVALID"


def test_status_api_exposes_stable_telegram_state_without_secrets(tmp_path: Path) -> None:
    settings, _store = _seed(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/status",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    telegram = body["data"]["telegram"]
    assert telegram["configured"] is True
    assert telegram["failing_receipts"] == 1
    assert telegram["receipts"] == {"queued": 1, "sending": 0, "sent": 1, "failed": 1}
    assert sorted(entry["chat_id"] for entry in telegram["per_chat"]) == [1001, 2002]
    assert telegram["recent_receipts"]
    assert any(item["message_id"] == 900 for item in telegram["recent_receipts"])
    assert any(
        item["last_error_code"] == "TELEGRAM_UPSTREAM_ERROR" for item in telegram["recent_receipts"]
    )
    rendered = response.text
    assert SENTINEL not in rendered
    assert RAW_BODY_SENTINEL not in rendered
    assert "never rendered in diagnostics" not in rendered


def test_ready_endpoint_carries_only_minimal_telegram_state(tmp_path: Path) -> None:
    settings, _store = _seed(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200, response.text
    telegram = response.json()["data"]["telegram"]
    # Readiness is unauthenticated: only counts/state, never chat ids or event
    # keys (those stay on the authenticated status/doctor surfaces).
    assert set(telegram) == {"configured", "failing_receipts", "receipts", "poller_state"}
    assert telegram["configured"] is True
    assert telegram["failing_receipts"] == 1
    assert telegram["receipts"] == {"queued": 1, "sending": 0, "sent": 1, "failed": 1}
    assert telegram["poller_state"] == "RUNNING"
    assert "1001" not in response.text
    assert "failed-1" not in response.text
    assert SENTINEL not in response.text


def test_queue_overflow_reaches_status_doctor_and_system_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, store = _seed(tmp_path)
    store.set_system_state(TELEGRAM_OUTBOX_OVERFLOW_KEY, TELEGRAM_QUEUE_FULL, updated_at=NOW_TEXT)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/status",
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["telegram"]["overflow_code"] == TELEGRAM_QUEUE_FULL

    _configure_cli_env(monkeypatch, tmp_path)
    runner = CliRunner()
    info = runner.invoke(cli_app, ["system", "info", "--json"])
    assert info.exit_code == 0, info.output
    assert json.loads(info.stdout)["data"]["telegram"]["overflow_code"] == TELEGRAM_QUEUE_FULL

    doctor = runner.invoke(cli_app, ["doctor", "--json"])
    assert doctor.exit_code == 0, doctor.output
    assert json.loads(doctor.stdout)["data"]["telegram"]["overflow_code"] == TELEGRAM_QUEUE_FULL


def test_unconfigured_telegram_with_failing_receipts_is_degraded(tmp_path: Path) -> None:
    settings = _settings(tmp_path, token=None)
    store = SqliteStore(settings)
    store.initialize()
    # No secret metadata and no env token: the channel is unconfigured. A
    # retained failed receipt still proves delivery is failing, so health must
    # degrade instead of reporting an optional channel as healthily idle.
    store.append_telegram_outbox(
        TelegramOutboxRecord(
            event_key="failed-unconfigured",
            kind="scan_report",
            chat_id=1001,
            reply_to_message_id=None,
            payload_json=json.dumps({"text": "never rendered in diagnostics"}),
            state="failed",  # type: ignore[arg-type]
            attempts=3,
            next_attempt_at=NOW_TEXT,
            message_id=None,
            last_error_code="TELEGRAM_UPSTREAM_ERROR",
            created_at=NOW_TEXT,
            sent_at=None,
        )
    )

    checks, _report = _checks(settings, store)

    assert checks["telegram"].status == "degraded"
    assert "failing telegram receipt" in checks["telegram"].detail
    assert "not configured" in checks["telegram"].detail


def _configure_cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    monkeypatch.setenv("OFT_TELEGRAM_BOT_TOKEN", SENTINEL)
    for name in (
        "OFT_OPENAI_API_KEY",
        "OFT_ANTHROPIC_API_KEY",
        "OFT_DEEPSEEK_API_KEY",
        "OFT_BINANCE_API_KEY",
        "OFT_BINANCE_API_SECRET",
        "OFT_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_system_info_and_doctor_json_expose_telegram_without_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _settings_value, _store = _seed(tmp_path)
    _configure_cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    info = runner.invoke(cli_app, ["system", "info", "--json"])
    assert info.exit_code == 0, info.output
    info_telegram = json.loads(info.stdout)["data"]["telegram"]
    assert info_telegram["failing_receipts"] == 1
    assert info_telegram["chat_count"] == 2

    doctor = runner.invoke(cli_app, ["doctor", "--json"])
    assert doctor.exit_code == 0, doctor.output
    doctor_payload = json.loads(doctor.stdout)["data"]
    assert doctor_payload["telegram"]["failing_receipts"] == 1
    assert any(
        check["name"] == "telegram" and check["status"] == "degraded"
        for check in doctor_payload["checks"]
    )

    for output in (info.output, doctor.output):
        assert SENTINEL not in output
        assert RAW_BODY_SENTINEL not in output
        assert "never rendered in diagnostics" not in output
