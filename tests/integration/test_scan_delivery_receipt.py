"""Scan/report ``delivery`` is a real receipt when configured, a stable stub otherwise."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import oft.application.secrets_service as secrets_module
from fakes.telegram import MemorySecretBackend
from oft.api.app import create_app
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
MARKET_FIXTURE = ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"

STUB = {
    "configured": False,
    "reason": "TELEGRAM_NOT_CONFIGURED: Telegram delivery is not configured in this build.",
}

SINGLE_PAYLOAD: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "period": "4h",
    "source": "backfill",
    "metrics_only": True,
}

BATCH_PAYLOAD: dict[str, Any] = {
    "period": "4h",
    "source": "backfill",
    "metrics_only": True,
}


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

    def post(self, payload: dict[str, Any], *, key: str = "scan-1") -> Any:
        return self.client.post(
            "/api/v1/scans",
            json=payload,
            headers={**self.auth, "Idempotency-Key": key},
        )


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
        allow_fixture_scan_debug=True,
    )


def _seed_backfill(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def _harness(tmp_path: Path) -> _Harness:
    settings = _settings(tmp_path)
    _seed_backfill(settings)
    app = create_app(settings, clock=FixedClock(NOW))
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def test_unconfigured_delivery_is_the_legacy_stub_on_every_surface(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.post(SINGLE_PAYLOAD)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        scan_id = data["scan"]["scan_id"]
        report_id = data["report_id"]

        assert data["delivery"] == STUB
        assert data["report"]["delivery"] == STUB
        detail = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        reports = harness.client.get(
            "/api/v1/reports", headers=harness.auth, params={"scan_id": scan_id}
        )
        scan_list = harness.client.get("/api/v1/scans", headers=harness.auth)
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    finally:
        harness.close()

    assert detail.json()["data"]["delivery"] == STUB
    assert scan_list.json()["data"]["items"][0]["delivery"] == STUB
    items = reports.json()["data"]["items"]
    assert items and items[0]["delivery"] == STUB
    assert report.json()["data"]["delivery"] == STUB


def test_configured_delivery_tracks_queued_then_sent_receipts(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.post(SINGLE_PAYLOAD)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        scan_id = data["scan"]["scan_id"]
        report_id = data["report_id"]
        event_key = f"scan:{scan_id}:summary"

        queued = data["delivery"]
        assert queued["configured"] is True
        assert isinstance(queued["reason"], str) and queued["reason"]
        assert queued["event_key"] == event_key
        assert queued["state"] == "queued"
        assert queued["attempts"] == 0
        assert queued["created_at"]
        assert data["report"]["delivery"]["event_key"] == event_key

        dispatcher = OutboxDispatcher(
            store=harness.container.store,
            clock=harness.container.clock,
            port_resolver=lambda: harness.container.telegram_port,
        )
        assert dispatcher.dispatch_once() == 1

        detail = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    finally:
        harness.close()

    sent = detail.json()["data"]["delivery"]
    assert sent["configured"] is True
    assert sent["state"] == "sent"
    assert isinstance(sent["message_id"], int)
    assert sent["sent_at"]
    assert isinstance(sent["reason"], str) and sent["reason"]
    report_delivery = report.json()["data"]["delivery"]
    assert report_delivery["configured"] is True
    assert report_delivery["event_key"] == event_key


def test_batch_report_detail_resolves_the_batch_summary_receipt(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        harness.configure()
        assert harness.container.store.set_starred("BTCUSDT", True, starred_at=utc_timestamp(NOW))
        response = harness.post(BATCH_PAYLOAD, key="batch-1")
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        batch_id = data["scan_id"]
        report_id = data["report_ids"][0]
        event_key = f"scan:{batch_id}:summary"

        assert data["delivery"]["configured"] is True
        assert data["delivery"]["event_key"] == event_key

        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.close()

    delivery = report.json()["data"]["delivery"]
    assert delivery["configured"] is True
    assert delivery["event_key"] == event_key
    item = next(row for row in listed.json()["data"]["items"] if row["scan_id"] == batch_id)
    assert item["delivery"]["event_key"] == event_key


def test_report_list_keeps_the_stub_while_detail_resolves_the_receipt(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    """PY-06: receipt resolution is detail-only, so a list is not an N+1."""
    harness = _harness(tmp_path)
    try:
        harness.configure()
        response = harness.post(SINGLE_PAYLOAD)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        scan_id = data["scan"]["scan_id"]
        report_id = data["report_id"]
        listed = harness.client.get(
            "/api/v1/reports", headers=harness.auth, params={"scan_id": scan_id}
        )
        detail = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    finally:
        harness.close()

    items = listed.json()["data"]["items"]
    assert items and items[0]["delivery"] == STUB
    delivery = detail.json()["data"]["delivery"]
    assert delivery["configured"] is True
    assert delivery["event_key"] == f"scan:{scan_id}:summary"


def test_fixture_path_scan_detail_keeps_the_legacy_shape(
    tmp_path: Path, backend: MemorySecretBackend
) -> None:
    harness = _harness(tmp_path)
    try:
        response = harness.post(
            {"symbol": "BTCUSDT", "period": "4h", "fixture_path": str(MARKET_FIXTURE)},
            key="fixture-1",
        )
        assert response.status_code == 201, response.text
        created = response.json()["data"]
        detail = harness.client.get(f"/api/v1/scans/{created['scan_id']}", headers=harness.auth)
    finally:
        harness.close()

    data = detail.json()["data"]
    assert data == created
    assert "delivery" not in data
