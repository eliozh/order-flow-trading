"""Manual scans share the durable job claim with scheduled scans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.application.jobs import JobLedger
from oft.application.scheduler import align_to_boundary
from oft.bootstrap import build_container
from oft.config import Settings
from oft.domain.time import utc_timestamp

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
PAYLOAD = {"symbol": "BTCUSDT", "period": "4h", "source": "backfill", "metrics_only": True}


@dataclass
class _Harness:
    settings: Settings
    app: Any
    client: TestClient
    auth: dict[str, str]

    @property
    def container(self) -> Any:
        return self.app.state.container

    def post(self, key: str = "idem") -> Any:
        return self.client.post(
            "/api/v1/scans",
            json=PAYLOAD,
            headers={**self.auth, "Idempotency-Key": key},
        )

    def close(self) -> None:
        self.client.close()


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )
    seeded = build_container(settings, initialize=True)
    result = seeded.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"
    app = create_app(settings)
    return _Harness(
        settings=settings,
        app=app,
        client=TestClient(app),
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def test_manual_scan_while_a_scheduled_scan_holds_the_claim_returns_409(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        ledger = JobLedger(store=harness.container.store, clock=harness.container.clock)
        now = harness.container.clock.now()
        window = align_to_boundary(now, "4h")
        claim = ledger.claim(
            "scan", utc_timestamp(window), owner="scheduled-1", trigger_source="SCHEDULED"
        )
        assert claim.claimed and claim.run is not None
        assert ledger.acquire_scan_lock(claim.run.run_id, ttl_seconds=900)

        response = harness.post("manual-conflict")
        scans = harness.container.store.count_scans()
        runs = harness.container.store.list_job_runs("scan")
    finally:
        harness.close()

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "SCAN_ALREADY_RUNNING"
    assert claim.run.run_id in body["error"]["message"]
    assert scans == 0
    assert [run.run_id for run in runs] == [claim.run.run_id]


def test_manual_scan_after_the_scheduled_run_finishes_succeeds(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        ledger = JobLedger(store=harness.container.store, clock=harness.container.clock)
        now = harness.container.clock.now()
        window = align_to_boundary(now, "4h")
        claim = ledger.claim(
            "scan", utc_timestamp(window), owner="scheduled-1", trigger_source="SCHEDULED"
        )
        assert claim.claimed and claim.run is not None
        assert ledger.acquire_scan_lock(claim.run.run_id, ttl_seconds=900)

        blocked = harness.post("manual-blocked")
        ledger.finish(claim.run.run_id, "DONE")
        ledger.release_scan_lock(claim.run.run_id)
        allowed = harness.post("manual-after")
        scans = harness.container.store.count_scans()
    finally:
        harness.close()

    assert blocked.status_code == 409
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["data"]["scan"]["status"] == "DONE"
    assert scans == 1


def test_manual_scan_is_blocked_by_a_running_scan_job_even_without_the_lock(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        ledger = JobLedger(store=harness.container.store, clock=harness.container.clock)
        now = harness.container.clock.now()
        window = align_to_boundary(now, "4h")
        claim = ledger.claim("scan", utc_timestamp(window), owner="scheduled-2")
        assert claim.claimed and claim.run is not None

        response = harness.post("manual-run-id")
    finally:
        harness.close()

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "SCAN_ALREADY_RUNNING"
    assert claim.run.run_id in body["error"]["message"]


def test_two_sequential_manual_scans_are_not_a_duplicate_job(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        first = harness.post("manual-1")
        second = harness.post("manual-2")
        scans = harness.container.store.count_scans()
        runs = harness.container.store.list_job_runs("scan")
    finally:
        harness.close()

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["report_id"] != second.json()["data"]["report_id"]
    assert scans == 2
    # Each manual run gets its own ledger row; the unique window key still
    # protects scheduled periodic windows.
    assert len(runs) == 2
    assert {run.status for run in runs} == {"DONE"}
    assert len({run.window_start for run in runs}) == 2
