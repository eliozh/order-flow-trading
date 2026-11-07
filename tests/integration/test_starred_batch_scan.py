"""Starred-symbol batch scan: per-symbol stages, reports, cost, and links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import oft.bootstrap as bootstrap
from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.application.jobs import JobLedger, ScheduledScanRunner
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.time import utc_timestamp
from oft.ports.llm import LLMRole
from oft.ports.store import ScanSymbolRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WINDOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
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


def _seed_symbol(container: Any, symbol: str, *, base_price: str = "3000") -> None:
    """Publish deterministic 15-minute aggTrades for one symbol, one week."""
    trades: list[AggTrade] = []
    trade_id = 1
    price = Decimal(base_price)
    for day_offset in range(7):
        day = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=day_offset)
        for slot in range(96):
            transact_time = day + timedelta(minutes=15 * slot)
            price = price + Decimal("0.50")
            trades.append(
                AggTrade(
                    symbol=symbol,
                    agg_trade_id=trade_id,
                    price=price,
                    quantity=Decimal("0.10") + Decimal(slot % 5) / Decimal("100"),
                    first_trade_id=trade_id * 10,
                    last_trade_id=trade_id * 10 + 9,
                    transact_time=transact_time,
                    is_buyer_maker=slot % 2 == 0,
                )
            )
            trade_id += 1
    entries = container.parquet.write_agg_trades_batch(trades, symbol=symbol, source="test")
    container.store.upsert_manifest_entries(entries)


def _star(container: Any, *symbols: str) -> None:
    for symbol in symbols:
        assert container.store.set_starred(symbol, True, starred_at=utc_timestamp(NOW))


def _seed(tmp_path: Path, *, second_symbol: str = "ETHUSDT") -> Settings:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    _seed_symbol(container, second_symbol)
    _star(container, "BTCUSDT", second_symbol)
    return settings


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]

    @property
    def container(self) -> Any:
        return self.app.state.container

    def post_batch(self, key: str = "batch") -> Any:
        return self.client.post(
            "/api/v1/scans",
            json=BATCH_PAYLOAD,
            headers={**self.auth, "Idempotency-Key": key},
        )


def _harness(settings: Settings) -> _Harness:
    app = create_app(settings)
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def test_two_starred_symbols_produce_one_batch_two_rows_and_two_reports(tmp_path: Path) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch()
        body = response.json()
        batch_id = body["data"]["scan_id"]
        detail = harness.client.get(f"/api/v1/scans/{batch_id}", headers=harness.auth)
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    data = body["data"]
    assert data["status"] == "COMPLETE"
    assert data["trigger_source"] == "MANUAL"
    assert data["period"] == "4h"
    assert data["symbols_total"] == 2
    assert data["symbols_completed"] == 2
    assert data["symbols_degraded"] == 0
    assert data["symbols_failed"] == 0
    assert data["cost_unpriced"] is True
    assert int(data["total_tokens"]) >= 0
    assert Decimal(data["total_cost_usd"]) >= 0
    assert data["delivery"]["configured"] is False
    assert "not configured" in data["delivery"]["reason"].lower()

    rows = {row["symbol"]: row for row in data["symbols"]}
    assert set(rows) == {"BTCUSDT", "ETHUSDT"}
    for row in rows.values():
        assert row["stage"] == "DONE"
        assert row["llm_status"] in {"VALID", "REPAIRED"}
        assert row["report_id"]

    assert len(data["report_links"]) == 2
    assert {link["symbol"] for link in data["report_links"]} == {"BTCUSDT", "ETHUSDT"}
    assert all(link["href"] == f"/reports/{link['report_id']}" for link in data["report_links"])

    # One batch row, one progress row per symbol, one report per symbol.
    assert harness.container.store.count_scans() == 2
    assert len(harness.container.store.list_reports()) == 2
    assert len(harness.container.store.list_scan_symbols(batch_id)) == 2

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["symbols"] == data["symbols"]

    items = {item["scan_id"]: item for item in listed.json()["data"]["items"]}
    assert items
    for item in items.values():
        assert item["llm_status"] in {"VALID", "REPAIRED"}
        assert item["batch_id"]


def test_rejected_llm_output_marks_only_that_symbol_degraded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    symbol_stub = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.ANALYSIS: "invalid_schema.json"},
    )
    valid_stub = LLMStub(fixtures_dir=FIXTURES)

    class SymbolAwareStub:
        def capabilities(self) -> Any:
            return valid_stub.capabilities()

        def complete(self, request: Any) -> Any:
            if "ETHUSDT" in request.user_prompt:
                return symbol_stub.complete(request)
            return valid_stub.complete(request)

        def estimate_cost(self, request: Any, model: str) -> Any:
            return valid_stub.estimate_cost(request, model)

    monkeypatch.setattr(
        bootstrap, "resolve_llm_provider", lambda settings, requested=None: SymbolAwareStub()
    )
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch()
        data = response.json()["data"]
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    assert data["status"] == "PARTIAL"
    assert data["symbols_completed"] == 1
    assert data["symbols_degraded"] == 1
    rows = {row["symbol"]: row for row in data["symbols"]}
    assert rows["BTCUSDT"]["stage"] == "DONE"
    assert rows["ETHUSDT"]["stage"] == "DEGRADED"
    assert rows["ETHUSDT"]["llm_status"] == "REJECTED"
    assert rows["ETHUSDT"]["report_id"]

    # The degraded symbol still has a CALC-only report and no opportunity.
    report = harness.container.store.get_report(rows["ETHUSDT"]["report_id"])
    assert report is not None
    assert report.llm_decision_id is not None
    assert len(harness.container.store.list_reports()) == 2
    opportunities = harness.container.store.list_opportunities()
    assert all("ETHUSDT" not in item.payload_json for item in opportunities)


def test_manual_batch_while_a_scan_is_running_returns_409(tmp_path: Path) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        ledger = JobLedger(store=harness.container.store, clock=harness.container.clock)
        claim = ledger.claim_scan(
            trigger_source="MANUAL", owner="running-scan", run_id="SCAN-RUNNING"
        )
        assert claim.claimed

        response = harness.post_batch("batch-conflict")
        scans = harness.container.store.count_scans()
        runs = harness.container.store.list_job_runs("scan")
    finally:
        harness.client.close()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_ALREADY_RUNNING"
    assert "SCAN-RUNNING" in response.json()["error"]["message"]
    assert scans == 0
    assert [run.run_id for run in runs] == ["SCAN-RUNNING"]


def test_reclaimed_batch_resumes_only_unfinished_symbols(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim(
        "scan", utc_timestamp(WINDOW), owner="crashed", trigger_source="SCHEDULED", lease_seconds=10
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="BTCUSDT",
            stage="DONE",
            started_at=utc_timestamp(NOW),
            finished_at=utc_timestamp(NOW),
            error_code=None,
            report_id="RPT-BTC-ALREADY-DONE",
            data_quality="COMPLETE",
        )
    )
    container.store.upsert_scan_symbol(
        ScanSymbolRecord(
            scan_id=run_id,
            symbol="ETHUSDT",
            stage="METRICS",
            started_at=utc_timestamp(NOW),
            finished_at=None,
            error_code=None,
            report_id=None,
            data_quality=None,
        )
    )

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).run_window(WINDOW, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["ETHUSDT"]
    assert outcome.failed_symbols == ()
    reports = container.store.list_reports()
    assert len(reports) == 1
    assert reports[0].symbol == "ETHUSDT"
    assert container.store.get_job_run(run_id).status == "DONE"  # type: ignore[union-attr]
    stages = {row.symbol: row.stage for row in container.store.list_scan_symbols(run_id)}
    assert stages == {"BTCUSDT": "DONE", "ETHUSDT": "DONE"}


def test_batch_detail_exposes_quality_window_and_per_symbol_stage_columns(
    tmp_path: Path,
) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch()
        batch_id = response.json()["data"]["scan_id"]
        detail = harness.client.get(f"/api/v1/scans/{batch_id}", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    data = detail.json()["data"]
    # S03.2 reads these fields; an empty payload rendered "Unknown data quality".
    assert data["data_quality"] in {"COMPLETE", "PARTIAL"}
    assert data["source_mode"] == "BACKFILL"
    assert data["window_start"]
    assert data["window_end"]
    assert data["trade_count"] > 0
    for row in data["symbols"]:
        assert row["data_stage"] == "DONE"
        assert row["metrics_stage"] == "DONE"
        assert row["llm_stage"] == "DONE"
        assert row["report_stage"] == "DONE"


def test_scan_list_is_batch_level_with_symbol_counts(tmp_path: Path) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch()
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["scan_id"] == response.json()["data"]["scan_id"]
    assert item["symbols_total"] == 2
    assert item["symbols_completed"] == 2
    assert {row["symbol"] for row in item["symbols"]} == {"BTCUSDT", "ETHUSDT"}
    assert len(item["symbols"]) == 2
    assert item["delivery"]["configured"] is False


def test_unsupported_source_is_still_rejected_as_validation_failed(tmp_path: Path) -> None:
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json={**BATCH_PAYLOAD, "symbol": "BTCUSDT", "source": "bogus"},
            headers={**harness.auth, "Idempotency-Key": "bogus-source"},
        )
    finally:
        harness.client.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
