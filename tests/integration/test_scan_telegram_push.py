"""Scan completion pushes: one idempotent Telegram summary per scan."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import oft.application.secrets_service as secrets_module
from fakes.telegram import MemorySecretBackend
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.api.app import create_app
from oft.application.jobs import SCAN_JOB_TYPE, JobLedger, ScheduledScanRunner
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import MAX_OUTBOUND_CHARS, SCAN_FAILED_ALERT_KEY, SCAN_REPORT_PUSH_KEY
from oft.domain.time import utc_timestamp
from oft.ports.store import ConfigRecord, ScanSymbolRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
WINDOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"

SCAN_PAYLOAD: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "period": "4h",
    "source": "backfill",
    "metrics_only": True,
}


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant = self.instant + timedelta(seconds=seconds)


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]

    @property
    def container(self) -> Any:
        return self.app.state.container

    def close(self) -> None:
        self.client.close()

    def post_scan(self, payload: dict[str, Any], *, key: str = "scan-1") -> Any:
        return self.client.post(
            "/api/v1/scans",
            json=payload,
            headers={**self.auth, "Idempotency-Key": key},
        )

    def configure(self) -> None:
        headers = self.auth
        token = self.client.put(
            "/api/v1/settings/telegram/token",
            json={"bot_token": SENTINEL},
            headers={**headers, "Idempotency-Key": "token-1"},
        )
        allowlist = self.client.put(
            "/api/v1/settings/telegram/allowlist",
            json={"chat_ids": [1001], "allowed_user_ids": [2002]},
            headers={**headers, "Idempotency-Key": "allow-1"},
        )
        assert token.status_code == 200, token.text
        assert allowlist.status_code == 200, allowlist.text


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> MemorySecretBackend:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    return memory


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )


def _seed_backfill(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def _set_config(container: Any, key: str, value: bool) -> None:
    container.store.set_config(
        ConfigRecord(
            key=key,
            value_json=json.dumps(value),
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )


def _harness(
    tmp_path: Path,
    *,
    clock: Any | None = None,
    backfill: bool = True,
) -> _Harness:
    settings = _settings(tmp_path)
    if backfill:
        _seed_backfill(settings)
    app = create_app(settings, clock=clock or FixedClock(NOW))
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _summary_text(record: Any) -> str:
    return json.loads(record.payload_json)["text"]


def test_manual_single_symbol_scan_enqueues_exactly_one_summary(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.post_scan(SCAN_PAYLOAD)
        assert response.status_code == 201, response.text
        scan_id = response.json()["data"]["scan"]["scan_id"]
        receipts = harness.container.store.list_telegram_receipts()
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.event_key == f"scan:{scan_id}:summary"
        assert receipt.kind == "scan_summary"
        assert receipt.chat_id == 1001
        text = _summary_text(receipt)
        assert text.splitlines()[0] == "SCAN COMPLETE"
        assert f"scan_id: {scan_id}" in text
        assert "symbols: 1/1" in text
        assert SENTINEL not in text
        assert "Traceback" not in text
        assert len(text) <= MAX_OUTBOUND_CHARS
        assert "error_message" not in json.loads(receipt.payload_json)

        # A duplicate completion notification is the store's idempotent no-op.
        harness.container.notifications.notify_scan(scan_id)
        assert len(harness.container.store.list_telegram_receipts()) == 1

        dispatcher = OutboxDispatcher(
            store=harness.container.store,
            clock=harness.container.clock,
            port_resolver=lambda: harness.container.telegram_port,
        )
        assert dispatcher.dispatch_once() == 1
        assert dispatcher.dispatch_once() == 0
        assert len(harness.container.telegram_port.sent) == 1
        assert len(harness.container.store.list_telegram_receipts()) == 1
    finally:
        harness.close()


def test_scheduled_run_window_enqueues_one_summary_and_a_duplicate_window_does_not(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    settings = _settings(tmp_path)
    _seed_backfill(settings)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    assert container.store.set_starred("BTCUSDT", True, starred_at=utc_timestamp(NOW))
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    runner = ScheduledScanRunner(container=container)

    outcome = runner.run_window(WINDOW, period="4h")

    assert outcome.claimed and outcome.run is not None
    scan_id = outcome.run.run_id
    receipts = container.store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].event_key == f"scan:{scan_id}:summary"
    assert _summary_text(receipts[0]).splitlines()[0] == "SCAN COMPLETE"

    again = runner.run_window(WINDOW, period="4h")
    assert again.claimed is False
    assert len(container.store.list_telegram_receipts()) == 1

    notifications = container.store.get_telegram_outbox(f"scan:{scan_id}:summary")
    assert notifications is not None
    assert notifications.state == "queued"


def test_an_unconfigured_channel_enqueues_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.post_scan(SCAN_PAYLOAD)
    finally:
        harness.close()

    assert response.status_code == 201, response.text
    assert harness.container.store.list_telegram_receipts() == ()


def test_a_toggled_off_scan_report_push_enqueues_nothing(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        _set_config(harness.container, SCAN_REPORT_PUSH_KEY, False)
        response = harness.post_scan(SCAN_PAYLOAD)
    finally:
        harness.close()

    assert response.status_code == 201, response.text
    assert harness.container.store.list_telegram_receipts() == ()


def test_an_enqueue_failure_never_changes_the_scan_or_report(
    tmp_path: Path, backend: MemorySecretBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()

        def _boom(self: SqliteStore, record: object) -> bool:
            raise RuntimeError("outbox unavailable")

        monkeypatch.setattr(SqliteStore, "append_telegram_outbox", _boom)
        response = harness.post_scan(SCAN_PAYLOAD)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        scan_id = data["scan"]["scan_id"]
        assert data["scan"]["status"] == "DONE"
        assert data["report_id"]
        assert data["delivery"]["configured"] is False
        assert harness.container.store.list_telegram_receipts() == ()

        detail = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        report = harness.client.get(f"/api/v1/reports/{data['report_id']}", headers=harness.auth)
        assert detail.status_code == 200
        assert detail.json()["data"]["status"] == "DONE"
        assert report.status_code == 200
        assert report.json()["data"]["report_id"] == data["report_id"]
    finally:
        harness.close()


def test_a_dispatch_failure_records_a_failed_receipt_but_not_a_failed_scan(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    clock = _MutableClock(NOW)
    harness = _harness(tmp_path, clock=clock)
    try:
        harness.configure()
        response = harness.post_scan(SCAN_PAYLOAD)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        scan_id = data["scan"]["scan_id"]
        event_key = f"scan:{scan_id}:summary"
        failing = TelegramFake(fail_send_times=10_000)
        dispatcher = OutboxDispatcher(
            store=harness.container.store,
            clock=clock,
            port_resolver=lambda: failing,
        )
        assert dispatcher.dispatch_once() == 0
        clock.advance(2)
        assert dispatcher.dispatch_once() == 0
        clock.advance(4)
        assert dispatcher.dispatch_once() == 0

        receipt = harness.container.store.get_telegram_outbox(event_key)
        assert receipt is not None
        assert receipt.state == "failed"
        assert receipt.last_error_code == "TELEGRAM_UPSTREAM_ERROR"

        detail = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        report = harness.client.get(f"/api/v1/reports/{data['report_id']}", headers=harness.auth)
        assert detail.json()["data"]["status"] == "DONE"
        assert report.status_code == 200
        delivery = detail.json()["data"]["delivery"]
    finally:
        harness.close()

    # The delivery contract reports the terminal failure, not a sent message.
    assert delivery["configured"] is True
    assert delivery["state"] == "failed"
    assert delivery["last_error_code"] == "TELEGRAM_UPSTREAM_ERROR"
    assert delivery["event_key"] == event_key


def test_an_unreadable_push_toggle_fails_closed(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        harness.container.store.set_config(
            ConfigRecord(
                key=SCAN_REPORT_PUSH_KEY,
                value_json='"not-a-boolean"',
                updated_at=NOW_TEXT,
                updated_by="test",
            )
        )
        response = harness.post_scan(SCAN_PAYLOAD)
    finally:
        harness.close()

    assert response.status_code == 201, response.text
    assert harness.container.store.list_telegram_receipts() == ()


def test_a_scan_without_reports_is_silent_by_default(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.post_scan({**SCAN_PAYLOAD, "symbol": "FTTUSDT"})
    finally:
        harness.close()

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
    assert harness.container.store.list_telegram_receipts() == ()


def test_a_failed_scan_without_reports_pushes_only_when_the_alert_is_enabled(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        _set_config(harness.container, SCAN_FAILED_ALERT_KEY, True)
        response = harness.post_scan({**SCAN_PAYLOAD, "symbol": "FTTUSDT", "period": "1h"})
        receipts = harness.container.store.list_telegram_receipts()
    finally:
        harness.close()

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
    assert len(receipts) == 1
    text = _summary_text(receipts[0])
    assert text.splitlines()[0] == "SCAN FAILED"
    assert "stage: DATA" in text
    assert "reason_code: DATA_UNAVAILABLE" in text
    assert "report_ids: none" in text
    # The failed-scan receipt carries the requested period, not the default 4h.
    assert "period: 1h" in text
    assert "period: 4h" not in text
    assert SENTINEL not in text


def test_a_resumed_already_complete_run_enqueues_exactly_one_receipt(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    settings = _settings(tmp_path)
    _seed_backfill(settings)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    assert container.store.set_starred("BTCUSDT", True, starred_at=utc_timestamp(NOW))

    ledger = JobLedger(store=container.store, clock=container.clock)
    claim = ledger.claim(
        SCAN_JOB_TYPE,
        utc_timestamp(WINDOW),
        trigger_source="SCHEDULED",
        owner="resume-owner",
        symbol="BTCUSDT",
        period="4h",
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=NOW_TEXT,
            finished_at=NOW_TEXT,
            error_code=None,
            report_id="RPT-RESUME-1",
            data_quality="COMPLETE",
            llm_status="VALID",
            cost_usd="0.10",
            cost_unpriced=False,
        )
    )

    runner = ScheduledScanRunner(container=container)
    first = runner.resume(run_id)

    assert first.reason == "already_complete"
    receipts = container.store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].event_key == f"scan:{run_id}:summary"
    assert _summary_text(receipts[0]).splitlines()[0] == "SCAN COMPLETE"

    # A second resume of the now-terminal run cannot enqueue another summary.
    second = runner.resume(run_id)
    assert second.claimed is False
    assert len(container.store.list_telegram_receipts()) == 1


def test_a_scheduled_partial_batch_enqueues_one_partial_summary(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    settings = _settings(tmp_path)
    _seed_backfill(settings)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    assert container.store.set_starred("BTCUSDT", True, starred_at=utc_timestamp(NOW))
    # ETHUSDT is in the universe but has no aggTrades archive: a real data gap.
    assert container.store.set_starred("ETHUSDT", True, starred_at=utc_timestamp(NOW))

    runner = ScheduledScanRunner(container=container)
    outcome = runner.run_window(WINDOW, period="4h")

    assert outcome.claimed and outcome.run is not None
    receipts = container.store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].event_key == f"scan:{outcome.run.run_id}:summary"
    text = _summary_text(receipts[0])
    assert text.splitlines()[0] == "SCAN PARTIAL"
    assert "symbols: 1/2" in text
    assert "failed: 1" in text
