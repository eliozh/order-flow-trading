"""Partial batch failures: a data gap fails one symbol, the batch continues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.api.envelope import success_envelope
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.config import Settings
from oft.domain.data_quality import DataGap, GapSeverity
from oft.domain.time import utc_timestamp
from oft.ports.store import ScanSymbolRecord  # noqa: F401 - documents the batch shape

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
GAP_START = "2026-10-01T00:00:00.000Z"
GAP_END = "2026-10-08T00:00:00.000Z"
BATCH_PAYLOAD = {
    "period": "4h",
    "source": "backfill",
    "metrics_only": False,
    "provider": "stub",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _seed(tmp_path: Path) -> Settings:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    # XAUUSDT is in the fixture registry (TRADING) but has no market data.
    container.store.upsert_data_gap(
        DataGap(
            gap_id="GAP-XAUUSDT-aggTrades-20261001",
            symbol="XAUUSDT",
            data_type="agg_trades",
            start=GAP_START,
            end=GAP_END,
            reason="MISSING_ARCHIVE",
            severity=GapSeverity.CRITICAL,
            detected_at=utc_timestamp(NOW),
            source="TEST",
        )
    )
    for symbol in ("BTCUSDT", "XAUUSDT"):
        assert container.store.set_starred(symbol, True, starred_at=utc_timestamp(NOW))
    return settings


@dataclass
class _Harness:
    app: Any
    client: TestClient
    auth: dict[str, str]

    @property
    def container(self) -> Any:
        return self.app.state.container


def _harness(settings: Settings) -> _Harness:
    app = create_app(settings)
    return _Harness(
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def test_data_gap_fails_one_symbol_with_the_exact_window_and_keeps_the_batch(
    tmp_path: Path,
) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json=BATCH_PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": "partial-batch"},
        )
        detail = harness.client.get(
            f"/api/v1/scans/{response.json()['data']['scan_id']}", headers=harness.auth
        )
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    data = detail.json()["data"]
    assert data["status"] == "PARTIAL"
    assert data["symbols_total"] == 2
    assert data["symbols_completed"] == 1
    assert data["symbols_failed"] == 1
    assert data["symbols_degraded"] == 0

    rows = {row["symbol"]: row for row in data["symbols"]}
    assert rows["BTCUSDT"]["stage"] == "DONE"
    assert rows["BTCUSDT"]["report_id"]
    failed = rows["XAUUSDT"]
    assert failed["stage"] == "FAILED"
    assert failed["error_code"] == "DATA_UNAVAILABLE"
    # The exact gap window is visible, not a generic failure.
    assert GAP_START in failed["error_message"]
    assert GAP_END in failed["error_message"]

    # The completed symbol keeps its report; the failed one has none.
    assert len(harness.container.store.list_reports()) == 1
    assert harness.container.store.list_reports()[0].symbol == "BTCUSDT"
    assert failed["report_id"] is None
    assert data["report_links"] == [
        {
            "symbol": "BTCUSDT",
            "report_id": rows["BTCUSDT"]["report_id"],
            "href": f"/reports/{rows['BTCUSDT']['report_id']}",
        }
    ]


def test_scan_list_includes_the_failed_symbol_in_its_batch_row(tmp_path: Path) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json=BATCH_PAYLOAD,
            headers={**harness.auth, "Idempotency-Key": "partial-list"},
        )
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    items = listed.json()["data"]["items"]
    assert len(items) == 1, "history is per batch, not per child scan"
    batch = items[0]
    assert batch["status"] == "PARTIAL"
    assert batch["symbols_failed"] == 1
    failed = {row["symbol"]: row for row in batch["symbols"]}["XAUUSDT"]
    assert failed["stage"] == "FAILED"
    assert GAP_START in failed["error_message"]


def test_cli_scan_run_exits_three_on_a_partial_batch(tmp_path: Path, monkeypatch: Any) -> None:
    import oft.cli.commands.scan as scan_module

    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    envelope = success_envelope(
        {
            "scan_id": "SCAN-BATCH-PARTIAL",
            "batch_id": "SCAN-BATCH-PARTIAL",
            "status": "PARTIAL",
            "trigger_source": "MANUAL",
            "period": "4h",
            "started_at": "2026-11-07T12:00:00.000Z",
            "finished_at": "2026-11-07T12:00:01.000Z",
            "symbols_total": 2,
            "symbols_completed": 1,
            "symbols_degraded": 0,
            "symbols_failed": 1,
            "total_tokens": 0,
            "total_cost_usd": "0",
            "cost_unpriced": True,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "stage": "DONE",
                    "report_id": "RPT-BTC-1",
                    "data_quality": "COMPLETE",
                    "error_code": None,
                    "error_message": None,
                    "llm_status": "VALID",
                },
                {
                    "symbol": "FTTUSDT",
                    "stage": "FAILED",
                    "report_id": None,
                    "data_quality": None,
                    "error_code": "DATA_UNAVAILABLE",
                    "error_message": f"Data gap {GAP_START} -> {GAP_END}",
                    "llm_status": None,
                },
            ],
            "report_links": [
                {"symbol": "BTCUSDT", "report_id": "RPT-BTC-1", "href": "/reports/RPT-BTC-1"}
            ],
            "delivery": {"configured": False, "reason": "Telegram is not configured."},
        },
        request_id="REQ-TEST",
        generated_at="2026-11-07T12:00:01.000Z",
        data_quality="PARTIAL",
    )

    class FakeClient:
        def create_scan(self, payload: dict[str, Any], idempotency_key: str | None = None) -> Any:
            assert "symbol" not in payload
            return envelope

    monkeypatch.setattr(
        scan_module.ApiClient,
        "from_settings",
        classmethod(lambda cls, settings: FakeClient()),
    )

    result = CliRunner().invoke(cli_app, ["scan", "run", "--period", "4h", "--json"])

    assert result.exit_code == 3, result.output
    assert "PARTIAL" in result.output
