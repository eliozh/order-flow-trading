"""Starred-symbol batch scan: per-symbol stages, reports, cost, and links."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import oft.application.jobs as jobs_module
import oft.bootstrap as bootstrap
from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.application.jobs import JobLedger, ScheduledScanRunner
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.time import utc_timestamp
from oft.ports.llm import CostEstimate, LLMRole, LLMUsage
from oft.ports.store import ScanRecord, ScanSymbolRecord

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


def _child_scan(
    scan_id: str,
    symbol: str,
    *,
    batch_id: str | None,
    created_at: str,
    trade_count: int = 7,
    data_quality: str = "COMPLETE",
) -> ScanRecord:
    return ScanRecord(
        scan_id=scan_id,
        symbol=symbol,
        period="4h",
        status="DONE",
        trigger_source="MANUAL",
        metrics_only=False,
        fixture_path="",
        started_at="2026-11-07T08:00:00.000Z",
        finished_at="2026-11-07T08:01:00.000Z",
        window_start="2026-10-01T00:00:00.000Z",
        window_end="2026-10-08T00:00:00.000Z",
        trade_count=trade_count,
        buy_volume="1",
        sell_volume="1",
        delta="0",
        cvd="0",
        data_quality=data_quality,
        manifest_path="part.parquet",
        parquet_rows=trade_count,
        created_at=created_at,
        report_id=None,
        source_mode="BACKFILL",
        batch_id=batch_id,
    )


def _done_row(scan_id: str, symbol: str) -> ScanSymbolRecord:
    return ScanSymbolRecord(
        scan_id=scan_id,
        symbol=symbol,
        stage="DONE",
        started_at=utc_timestamp(NOW),
        finished_at=utc_timestamp(NOW),
        error_code=None,
        report_id=f"RPT-{symbol}",
        data_quality="COMPLETE",
    )


def test_batch_children_survive_more_than_500_newer_scans(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    run_id = "SCAN-OLD-BATCH"
    claim = ledger.claim(
        "scan",
        utc_timestamp(WINDOW),
        trigger_source="MANUAL",
        owner="old-batch",
        run_id=run_id,
    )
    assert claim.claimed
    for symbol in ("BTCUSDT", "ETHUSDT"):
        container.store.save_scan(
            _child_scan(
                f"SCAN-{symbol}-CHILD",
                symbol,
                batch_id=run_id,
                created_at="2026-11-07T08:00:00.000Z",
                trade_count=7,
            )
        )
        container.store.upsert_scan_symbol(_done_row(run_id, symbol))
    # Push the batch's children out of any "newest 500 scans" window.
    base = NOW + timedelta(hours=1)
    for index in range(520):
        container.store.save_scan(
            _child_scan(
                f"SCAN-FILLER-{index:04d}",
                "SOLUSDT",
                batch_id=None,
                created_at=utc_timestamp(base + timedelta(seconds=index)),
            )
        )

    harness = _harness(settings)
    try:
        detail = harness.client.get(f"/api/v1/scans/{run_id}", headers=harness.auth)
        listed = harness.client.get("/api/v1/scans", headers=harness.auth)
    finally:
        harness.client.close()

    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["trade_count"] == 14
    assert data["window_start"] == "2026-10-01T00:00:00.000Z"
    assert data["window_end"] == "2026-10-08T00:00:00.000Z"
    assert data["data_quality"] == "COMPLETE"
    assert data["source_mode"] == "BACKFILL"
    assert data["period"] == "4h"
    assert {row["symbol"] for row in data["symbols"]} == {"BTCUSDT", "ETHUSDT"}
    items = listed.json()["data"]["items"]
    assert any(item["scan_id"] == run_id for item in items)


def test_resume_metrics_only_partial_batch_skips_the_llm(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL",
        owner="manual-owner",
        run_id="SCAN-METRICS-ONLY",
        metrics_only=True,
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(_done_row(run_id, "BTCUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["ETHUSDT"]
    assert container.store.list_llm_calls() == ()
    assert container.store.get_job_run(run_id).status == "DONE"  # type: ignore[union-attr]


def _single_symbol_claim(container: Any, *, run_id: str, **options: Any) -> Any:
    """Claim a crashed single-symbol run, mirroring the POST path's scope."""
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL",
        owner=run_id,
        run_id=run_id,
        **options,
    )
    assert claim.claimed and claim.run is not None
    return claim.run


def _non_terminal_row(scan_id: str, symbol: str, stage: str) -> ScanSymbolRecord:
    return ScanSymbolRecord(
        scan_id=scan_id,
        symbol=symbol,
        stage=stage,
        started_at=utc_timestamp(NOW),
        finished_at=None,
        error_code=None,
        report_id=None,
        data_quality=None,
    )


def test_resume_single_symbol_metrics_only_live_keeps_scope_and_options(
    tmp_path: Path,
) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    run = _single_symbol_claim(
        container,
        run_id="SCAN-SINGLE-LIVE",
        source="live",
        metrics_only=True,
        provider="stub",
        symbol="BTCUSDT",
    )
    persisted = container.store.get_job_run(run.run_id)
    assert persisted is not None
    assert persisted.symbol == "BTCUSDT"
    assert persisted.source == "live"
    assert persisted.metrics_only is True
    assert persisted.provider == "stub"
    container.store.upsert_scan_symbol(_non_terminal_row(run.run_id, "BTCUSDT", "METRICS"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run.run_id, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["BTCUSDT"]
    assert outcome.scans[0].source_mode == "LIVE"
    assert outcome.scans[0].metrics_only is True
    assert [report.symbol for report in container.store.list_reports()] == ["BTCUSDT"]
    assert container.store.list_llm_calls() == ()
    stages = {row.symbol: row.stage for row in container.store.list_scan_symbols(run.run_id)}
    assert stages == {"BTCUSDT": "DONE"}
    assert container.store.get_job_run(run.run_id).status == "DONE"  # type: ignore[union-attr]


def test_resume_single_symbol_llm_run_scans_only_its_symbol(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    run = _single_symbol_claim(
        container,
        run_id="SCAN-SINGLE-LLM",
        source="backfill",
        metrics_only=False,
        provider="stub",
        symbol="BTCUSDT",
    )
    container.store.upsert_scan_symbol(_non_terminal_row(run.run_id, "BTCUSDT", "LLM"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run.run_id, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["BTCUSDT"]
    assert [report.symbol for report in container.store.list_reports()] == ["BTCUSDT"]
    assert container.store.list_llm_calls()  # the LLM stage ran for the scoped symbol
    assert {row.symbol for row in container.store.list_scan_symbols(run.run_id)} == {"BTCUSDT"}
    assert container.store.get_job_run(run.run_id).status == "DONE"  # type: ignore[union-attr]


def test_reclaimed_batch_runs_a_symbol_that_never_wrote_a_row(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim(
        "scan", utc_timestamp(WINDOW), owner="crashed", trigger_source="SCHEDULED", lease_seconds=10
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(_done_row(run_id, "BTCUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).run_window(WINDOW, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["ETHUSDT"]
    assert container.store.get_job_run(run_id).status == "DONE"  # type: ignore[union-attr]


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


def _failed_row(
    scan_id: str, symbol: str, error_code: str = "DATA_UNAVAILABLE"
) -> ScanSymbolRecord:
    return ScanSymbolRecord(
        scan_id=scan_id,
        symbol=symbol,
        stage="FAILED",
        started_at=utc_timestamp(NOW),
        finished_at=utc_timestamp(NOW),
        error_code=error_code,
        error_message="exact gap window",
        report_id=None,
        data_quality=None,
    )


def test_one_raising_symbol_is_isolated_and_the_job_is_partial(
    tmp_path: Path, monkeypatch: Any
) -> None:
    real_service = jobs_module.ScanService

    class FlakyScanService(real_service):  # type: ignore[misc, valid-type]
        def run(self, request: Any, *, scan_id: str | None = None) -> Any:
            if request.symbol == "ETHUSDT":
                raise RuntimeError("symbol exploded")
            return super().run(request, scan_id=scan_id)

    monkeypatch.setattr(jobs_module, "ScanService", FlakyScanService)
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch("batch-one-raises")
        data = response.json()["data"]
        batch_id = data["scan_id"]
        job = harness.container.store.get_job_run(batch_id)
        rows = {row.symbol: row for row in harness.container.store.list_scan_symbols(batch_id)}
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    assert data["status"] == "PARTIAL"
    assert data["symbols_completed"] == 1
    assert data["symbols_failed"] == 1
    assert rows["BTCUSDT"].stage == "DONE"
    assert rows["ETHUSDT"].stage == "FAILED"
    assert rows["ETHUSDT"].error_code == "SCAN_FAILED"
    assert job is not None
    assert job.status == "PARTIAL"


def test_a_batch_where_every_symbol_fails_is_persisted_failed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class ExplodingScanService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self, request: Any, *, scan_id: str | None = None) -> Any:
            raise RuntimeError("all symbols exploded")

    monkeypatch.setattr(jobs_module, "ScanService", ExplodingScanService)
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.client.post(
            "/api/v1/scans",
            json={**BATCH_PAYLOAD, "period": "1h"},
            headers={**harness.auth, "Idempotency-Key": "batch-all-fail"},
        )
        data = response.json()["data"]
        batch_id = data["scan_id"]
        detail = harness.client.get(f"/api/v1/scans/{batch_id}", headers=harness.auth)
        job = harness.container.store.get_job_run(batch_id)
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    assert data["status"] == "FAILED"
    assert data["symbols_completed"] == 0
    assert data["symbols_failed"] == 2
    assert job is not None
    assert job.status == "FAILED"
    # No child scan exists, so the batch period comes from the persisted run.
    assert job.period == "1h"
    assert detail.json()["data"]["period"] == "1h"


def test_resume_with_all_rows_failed_finishes_the_job_failed(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-ALL-FAILED"
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    for symbol in ("BTCUSDT", "ETHUSDT"):
        container.store.upsert_scan_symbol(_failed_row(run_id, symbol))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert outcome.scans == ()
    assert container.store.get_job_run(run_id).status == "FAILED"  # type: ignore[union-attr]


def test_resume_with_mixed_terminal_rows_finishes_the_job_partial(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-MIXED-FAILED"
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(_done_row(run_id, "BTCUSDT"))
    container.store.upsert_scan_symbol(_failed_row(run_id, "ETHUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert outcome.scans == ()
    assert container.store.get_job_run(run_id).status == "PARTIAL"  # type: ignore[union-attr]


def test_resume_merges_a_prior_failed_row_into_the_job_status(tmp_path: Path) -> None:
    """A prior owner's FAILED row must downgrade a resumed run from DONE to PARTIAL."""
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-PROBE-PARTIAL"
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    # Probe: BTCUSDT was checkpointed FAILED by the prior owner; ETHUSDT has no row.
    container.store.upsert_scan_symbol(_failed_row(run_id, "BTCUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["ETHUSDT"]
    assert outcome.failed_symbols == ()
    persisted = container.store.get_job_run(run_id)
    assert persisted is not None and persisted.status == "PARTIAL"
    stages = {row.symbol: row.stage for row in container.store.list_scan_symbols(run_id)}
    assert stages == {"BTCUSDT": "FAILED", "ETHUSDT": "DONE"}


def test_resume_with_every_terminal_row_done_persists_done(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-PROBE-DONE"
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(_done_row(run_id, "BTCUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert [scan.symbol for scan in outcome.scans] == ["ETHUSDT"]
    persisted = container.store.get_job_run(run_id)
    assert persisted is not None and persisted.status == "DONE"


def test_resume_where_the_resumed_symbol_also_fails_persists_failed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class ExplodingScanService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self, request: Any, *, scan_id: str | None = None) -> Any:
            raise RuntimeError("resumed symbol exploded")

    monkeypatch.setattr(jobs_module, "ScanService", ExplodingScanService)
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL", owner="manual-owner", run_id="SCAN-PROBE-FAILED"
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    container.store.upsert_scan_symbol(_failed_row(run_id, "BTCUSDT"))

    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert outcome.scans == ()
    assert outcome.failed_symbols == ("ETHUSDT",)
    persisted = container.store.get_job_run(run_id)
    assert persisted is not None and persisted.status == "FAILED"


def test_interrupted_one_hour_batch_resumes_with_its_persisted_period(tmp_path: Path) -> None:
    settings = _seed(tmp_path)
    container = build_container(settings, initialize=True)
    ledger = JobLedger(store=container.store, clock=FixedClock(NOW))
    claim = ledger.claim_scan(
        trigger_source="MANUAL",
        owner="manual-owner",
        run_id="SCAN-INTERRUPTED-1H",
        period="1h",
    )
    assert claim.claimed and claim.run is not None
    run_id = claim.run.run_id
    persisted = container.store.get_job_run(run_id)
    assert persisted is not None and persisted.period == "1h"
    container.store.upsert_scan_symbol(_non_terminal_row(run_id, "BTCUSDT", "METRICS"))

    # The scheduler restarts with a different active period; the interrupted
    # run must still finish the window it started with.
    later = FixedClock(NOW + timedelta(seconds=60))
    outcome = ScheduledScanRunner(container=container, clock=later).resume(run_id, period="4h")

    assert outcome.claimed is True
    assert {scan.period for scan in outcome.scans} == {"1h"}
    reports = container.store.list_reports()
    assert reports and {report.timeframe for report in reports} == {"1h"}

    harness = _harness(settings)
    try:
        detail = harness.client.get(f"/api/v1/scans/{run_id}", headers=harness.auth)
    finally:
        harness.client.close()

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["period"] == "1h"


def test_provider_exception_degrades_only_that_symbol_and_keeps_the_report(
    tmp_path: Path, monkeypatch: Any
) -> None:
    valid_stub = LLMStub(fixtures_dir=FIXTURES)

    class RaisingForEth:
        def capabilities(self) -> Any:
            return valid_stub.capabilities()

        def complete(self, request: Any) -> Any:
            if "ETHUSDT" in request.user_prompt:
                raise RuntimeError("provider unavailable")
            return valid_stub.complete(request)

        def estimate_cost(self, request: Any, model: str) -> Any:
            return valid_stub.estimate_cost(request, model)

    monkeypatch.setattr(
        bootstrap, "resolve_llm_provider", lambda settings, requested=None: RaisingForEth()
    )
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch("batch-provider-raises")
        data = response.json()["data"]
        rows = {row["symbol"]: row for row in data["symbols"]}
        report = harness.container.store.get_report(rows["ETHUSDT"]["report_id"])
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    assert data["status"] == "PARTIAL"
    assert rows["BTCUSDT"]["stage"] == "DONE"
    assert rows["ETHUSDT"]["stage"] == "DEGRADED"
    assert rows["ETHUSDT"]["llm_status"] == "REJECTED"
    assert report is not None
    llm = json.loads(report.llm_json or "{}")
    assert str(llm.get("reason", "")).startswith("LLM_STAGE_FAILED")


def test_batch_aggregates_priced_usage_tokens_and_cost(tmp_path: Path, monkeypatch: Any) -> None:
    valid_stub = LLMStub(fixtures_dir=FIXTURES)

    class PricedProvider:
        def capabilities(self) -> Any:
            return valid_stub.capabilities()

        def complete(self, request: Any) -> Any:
            base = valid_stub.complete(request)
            return replace(
                base,
                provider="priced",
                model="priced-test",
                usage=LLMUsage(input_tokens=100, output_tokens=50),
            )

        def estimate_cost(self, request: Any, model: str) -> Any:
            return CostEstimate(amount_usd="0.0015", price_version="test/v1", unpriced=False)

    monkeypatch.setattr(
        bootstrap, "resolve_llm_provider", lambda settings, requested=None: PricedProvider()
    )
    harness = _harness(_seed(tmp_path))
    try:
        response = harness.post_batch("batch-priced-usage")
        data = response.json()["data"]
    finally:
        harness.client.close()

    assert response.status_code == 201, response.text
    assert data["total_tokens"] == 300
    assert Decimal(data["total_cost_usd"]) == Decimal("0.0030")
    assert data["cost_unpriced"] is False
    assert [row["llm_tokens"] for row in data["symbols"]] == [150, 150]
    assert all(row["cost_unpriced"] is False for row in data["symbols"])
