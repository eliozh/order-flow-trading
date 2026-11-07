"""Metrics-only manual scan: DATA -> METRICS -> REPORT through API and CLI."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.adapters.sqlite_store import database_path
from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
CONTRACTS = ROOT / "docs" / "contracts"
START = date(2026, 10, 1)
END = date(2026, 10, 7)

FIXTURE = ROOT / "tests" / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"

SCAN_PAYLOAD: dict[str, Any] = {
    "symbol": "BTCUSDT",
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


def _settings(tmp_path: Path, *, env: str = "test", debug: bool = False) -> Settings:
    return Settings(
        env=env,
        host="testserver" if env != "test" else "127.0.0.1",
        port=80 if env != "test" else 8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        allow_fixture_scan_debug=debug,
    )


def _seed_backfill(settings: Settings) -> None:
    container = build_container(settings, initialize=True)
    result = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def _harness(
    tmp_path: Path,
    *,
    backfill: bool = True,
    env: str = "test",
    debug: bool = False,
) -> _Harness:
    settings = _settings(tmp_path, env=env, debug=debug)
    if backfill:
        _seed_backfill(settings)
    app = create_app(settings)
    client = TestClient(app)
    return _Harness(
        settings=settings,
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
    )


def _post(
    harness: _Harness,
    *,
    key: str | None,
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = dict(harness.auth)
    if key is not None:
        headers["Idempotency-Key"] = key
    return harness.client.post("/api/v1/scans", json=payload or SCAN_PAYLOAD, headers=headers)


def _run_scan(harness: _Harness, key: str = "idem-1") -> dict[str, Any]:
    response = _post(harness, key=key)
    assert response.status_code == 201, response.text
    return response.json()


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    common = _load("common.schema.json")
    metrics_schema = _load("orderflow-metrics.schema.json")
    registry = (
        Registry()
        .with_resource(
            common["$id"], Resource.from_contents(common, default_specification=DRAFT202012)
        )
        .with_resource(
            metrics_schema["$id"],
            Resource.from_contents(metrics_schema, default_specification=DRAFT202012),
        )
    )
    snapshot = Draft202012Validator(_load("market-snapshot.schema.json"), registry=registry)
    metrics = Draft202012Validator(metrics_schema, registry=registry)
    return snapshot, metrics


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _report(harness: _Harness, scan_payload: dict[str, Any]) -> dict[str, Any]:
    report_id = scan_payload["report_id"]
    response = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_post_requires_idempotency_key(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = _post(harness, key=None)
    finally:
        harness.close()

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_same_key_replays_the_original_response_without_rerunning(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        first = _post(harness, key="idem-same")
        second = _post(harness, key="idem-same")

        assert first.status_code == second.status_code == 201
        first_body = first.json()
        second_body = second.json()
        assert second_body["meta"]["idempotent_replay"] is True
        assert first_body["meta"]["idempotent_replay"] is False
        assert second_body["data"] == first_body["data"]
        assert len(harness.container.store.list_scans()) == 1
        assert len(harness.container.store.list_reports()) == 1
    finally:
        harness.close()


def test_same_key_with_a_different_payload_conflicts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        assert _post(harness, key="idem-conflict").status_code == 201
        response = _post(harness, key="idem-conflict", payload={**SCAN_PAYLOAD, "period": "1h"})
    finally:
        harness.close()

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_metrics_only_scan_produces_a_report_and_no_llm_call(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        body = _run_scan(harness)
        data = body["data"]
        assert data["report_id"].startswith("RPT-BTCUSDT-")
        assert data["scan"]["status"] == "DONE"
        assert data["scan"]["source_mode"] == "BACKFILL"
        assert data["report"]["llm_decision_id"] is None
        assert data["report"]["narrative_ref"] is None
        assert data["report"]["data_quality"] in {"COMPLETE", "PARTIAL", "ESTIMATED"}

        report = _report(harness, data)
        assert report["llm_decision_id"] is None
        assert report["llm"]["configured"] is False
        assert report["llm"]["reason"]
        assert report["metrics_manifest"]
        assert {entry["data_type"] for entry in report["metrics_manifest"]} >= {
            "bars",
            "metrics",
            "footprint",
        }

        # No LLM table or artifact exists in this metrics-only slice.
        with sqlite3.connect(database_path(harness.settings)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert "llm_calls" not in tables
        assert body["meta"]["provenance"] == "CALC"
    finally:
        harness.close()


def test_get_scan_and_report_use_valid_calc_envelopes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        body = _run_scan(harness)
        scan_id = body["data"]["scan"]["scan_id"]
        report_id = body["data"]["report_id"]

        scan_response = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth)
        report_response = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth)
    finally:
        harness.close()

    assert scan_response.status_code == 200
    scan = scan_response.json()
    assert scan["ok"] is True and scan["error"] is None
    assert scan["meta"]["provenance"] == "CALC"
    assert scan["data"]["scan_id"] == scan_id
    assert scan["data"]["report_id"] == report_id

    assert report_response.status_code == 200
    report = report_response.json()
    assert report["ok"] is True and report["error"] is None
    assert report["meta"]["provenance"] == "CALC"
    assert report["data"]["report_id"] == report_id


def test_report_embeds_valid_snapshot_and_metrics_that_agree(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        body = _run_scan(harness)
        report = _report(harness, body["data"])
    finally:
        harness.close()

    snapshot_validator, metrics_validator = _validators()
    _validate(snapshot_validator, report["snapshot"])
    _validate(metrics_validator, report["metrics"])

    snapshot = report["snapshot"]
    metrics = report["metrics"]
    points = {point["bar_start"]: point for point in metrics["cvd"]["points"]}
    assert snapshot["orderflow"] == metrics
    assert snapshot["bars"]
    for bar in snapshot["bars"]:
        point = points[bar["bar_start"]]
        assert bar["delta"] == point["delta"]
        assert bar["cvd"] == point["cvd"]
        assert Decimal(bar["volume"]) == Decimal(point["buy_volume"]) + Decimal(
            point["sell_volume"]
        )
        assert bar["taker_buy_volume"] == point["buy_volume"]
    assert snapshot["snapshot_hash"]


def test_second_manual_request_while_running_is_conflict(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        acquired = harness.app.state.scan_lock.acquire(blocking=False)
        assert acquired
        try:
            response = _post(harness, key="idem-running")
        finally:
            harness.app.state.scan_lock.release()
    finally:
        harness.close()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_ALREADY_RUNNING"


def test_scan_without_backfill_is_data_unavailable(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)
    try:
        response = _post(harness, key="idem-nodata")
    finally:
        harness.close()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"


def test_unsupported_source_is_validation_failed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = _post(
            harness,
            key="idem-source",
            payload={**SCAN_PAYLOAD, "source": "live"},
        )
    finally:
        harness.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_scan_and_report_lists_are_enveloped(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        body = _run_scan(harness)
        scans = harness.client.get("/api/v1/scans", headers=harness.auth)
        reports = harness.client.get("/api/v1/reports", headers=harness.auth)
    finally:
        harness.close()

    assert scans.status_code == 200
    scans_body = scans.json()
    assert scans_body["ok"] is True
    assert scans_body["data"]["total"] == 1
    assert scans_body["data"]["items"][0]["scan_id"] == body["data"]["scan"]["scan_id"]

    assert reports.status_code == 200
    reports_body = reports.json()
    assert reports_body["ok"] is True
    assert reports_body["data"]["total"] == 1
    assert reports_body["data"]["items"][0]["report_id"] == body["data"]["report_id"]


def _cli_environment(monkeypatch: pytest.MonkeyPatch, live_daemon: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(live_daemon.port))
    monkeypatch.setenv("OFT_DATA_DIR", str(live_daemon.data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))


def _seed_daemon_backfill(settings: Settings) -> None:
    seeded = settings.model_copy(update={"archive_fixture_dir": ARCHIVE})
    container = build_container(seeded, initialize=True)
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)


def test_cli_scan_run_report_list_and_show(
    live_daemon: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_environment(monkeypatch, live_daemon)
    _seed_daemon_backfill(live_daemon)
    runner = CliRunner()

    run_result = runner.invoke(
        cli_app,
        [
            "scan",
            "run",
            "--metrics-only",
            "--symbol",
            "BTCUSDT",
            "--period",
            "4h",
            "--source",
            "backfill",
            "--json",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.stdout)
    assert run_payload["ok"] is True
    report_id = run_payload["data"]["report_id"]
    assert report_id.startswith("RPT-BTCUSDT-")
    scan_id = run_payload["data"]["scan"]["scan_id"]

    status_result = runner.invoke(cli_app, ["scan", "status", scan_id, "--json"])
    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.stdout)["data"]["report_id"] == report_id

    list_result = runner.invoke(cli_app, ["report", "list", "--json"])
    assert list_result.exit_code == 0, list_result.output
    list_payload = json.loads(list_result.stdout)
    assert list_payload["data"]["items"][0]["report_id"] == report_id

    show_json = runner.invoke(cli_app, ["report", "show", report_id, "--json"])
    assert show_json.exit_code == 0, show_json.output
    show_payload = json.loads(show_json.stdout)
    assert show_payload["data"]["report_id"] == report_id
    assert show_payload["data"]["metrics"]["cvd"]["points"]

    show_table = runner.invoke(cli_app, ["report", "show", report_id])
    assert show_table.exit_code == 0, show_table.output
    output = show_table.stdout
    assert "CALC" in output
    assert "CVD" in output
    assert "LLM not configured" in output
    assert "Volume Profile" in output or "POC" in output
    assert "Liquidations" in output
    assert output.index("CALC") < output.index("LLM not configured")

    metrics = show_payload["data"]["metrics"]
    assert metrics["volume_profile"]["poc"] in output
    assert metrics["volume_profile"]["vah"] in output
    assert metrics["volume_profile"]["val"] in output
    assert f"{len(metrics['footprint']['bars'])} bars" in output
    assert f"{metrics['liquidations']['count']} events" in output
    assert metrics["liquidations"]["coverage"] in output
    if metrics["open_interest"]["points"]:
        assert metrics["open_interest"]["delta_over_window"] in output
    else:
        assert "OI Δ              not measured" in output
    if metrics["funding"]["history"]:
        assert metrics["funding"]["current_rate"] in output
        assert metrics["funding"]["next_funding_time"] in output
    else:
        assert "Funding           not measured" in output


def test_cli_report_show_unknown_report_exits_one(
    live_daemon: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_environment(monkeypatch, live_daemon)
    result = CliRunner().invoke(cli_app, ["report", "show", "RPT-DOES-NOT-EXIST", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "NOT_FOUND"


def test_report_ids_are_unique_within_the_same_second() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from oft.application.scan_service import _report_id

    instant = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = set(pool.map(lambda _: _report_id("BTCUSDT", instant), range(100)))

    assert len(ids) == 100
    assert all(report_id.startswith("RPT-BTCUSDT-") for report_id in ids)


def test_rapid_sequential_scans_create_distinct_reports(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        first = _post(harness, key="rapid-1")
        second = _post(harness, key="rapid-2")
        reports = harness.container.store.list_reports()
        page = harness.client.get("/api/v1/scans?limit=1&offset=0", headers=harness.auth).json()
    finally:
        harness.close()

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["report_id"] != second.json()["data"]["report_id"]
    assert len(reports) == 2
    assert page["data"]["total"] == 2
    assert page["meta"]["pagination"]["has_next"] is True
    assert page["meta"]["pagination"]["next_cursor"] == "1"


def test_expired_idempotency_key_runs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.api.routes.scans as scans_routes

    monkeypatch.setattr(scans_routes, "_IDEMPOTENCY_TTL", timedelta(seconds=-1))
    harness = _harness(tmp_path)
    try:
        first = _post(harness, key="expired")
        second = _post(harness, key="expired")
        reports = harness.container.store.list_reports()
    finally:
        harness.close()

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["meta"]["idempotent_replay"] is False
    assert second.json()["data"]["report_id"] != first.json()["data"]["report_id"]
    assert len(reports) == 2


def _fixture_scan(harness: _Harness) -> Any:
    return _post(
        harness,
        key=None,
        payload={
            "fixture_path": str(FIXTURE),
            "symbol": "BTCUSDT",
            "period": "4h",
            "metrics_only": True,
        },
    )


def test_fixture_debug_path_is_rejected_outside_the_test_environment(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False, env="paper", debug=True)
    try:
        response = _fixture_scan(harness)
    finally:
        harness.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_fixture_debug_path_requires_the_explicit_flag(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False, env="test", debug=False)
    try:
        response = _fixture_scan(harness)
    finally:
        harness.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_fixture_debug_path_is_allowed_with_the_flag_in_tests(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False, env="test", debug=True)
    try:
        response = _fixture_scan(harness)
    finally:
        harness.close()

    assert response.status_code == 201
    assert response.json()["data"]["data_quality"]


def test_snapshot_ids_are_unique_within_the_same_second() -> None:
    from oft.application.scan_service import _snapshot_id

    instant = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    ids = {_snapshot_id("BTCUSDT", instant) for _ in range(100)}
    assert len(ids) == 100
    assert all(snapshot_id.startswith("SNAP-BTCUSDT-") for snapshot_id in ids)


def test_reports_pagination_reports_offset_total_and_next_cursor(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        assert _post(harness, key="page-1").status_code == 201
        assert _post(harness, key="page-2").status_code == 201
        first = harness.client.get("/api/v1/reports?limit=1&offset=0", headers=harness.auth).json()
        second = harness.client.get("/api/v1/reports?limit=1&offset=1", headers=harness.auth).json()
    finally:
        harness.close()

    assert first["data"]["total"] == 2
    assert first["meta"]["pagination"]["total"] == 2
    assert first["meta"]["pagination"]["has_next"] is True
    assert first["meta"]["pagination"]["next_cursor"] == "1"
    assert second["meta"]["pagination"]["has_next"] is False
    assert second["meta"]["pagination"]["next_cursor"] is None
    assert len(second["data"]["items"]) == 1


def test_stale_running_scan_is_failed_on_read(tmp_path: Path) -> None:
    from dataclasses import replace

    harness = _harness(tmp_path)
    try:
        assert _post(harness, key="stale-source").status_code == 201
        record = harness.container.store.list_scans(limit=1)[0]
        stale = replace(
            record,
            scan_id="SCAN-STALE",
            status="RUNNING",
            started_at="2000-01-01T00:00:00.000Z",
            finished_at="",
            report_id=None,
        )
        harness.container.store.save_scan(stale)
        response = harness.client.get("/api/v1/scans/SCAN-STALE", headers=harness.auth)
    finally:
        harness.close()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "FAILED"
    assert data["error_code"] == "SCAN_STALE"
    assert "exceeded" in data["error_message"]


def test_report_and_scan_are_written_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("report insert failed")

        monkeypatch.setattr(harness.container.store, "_insert_report", fail)
        with pytest.raises(RuntimeError):
            _post(harness, key="atomic-failure")
        reports = harness.container.store.list_reports()
        scans = harness.container.store.list_scans()
    finally:
        harness.close()

    assert reports == ()
    assert len(scans) == 1
    assert scans[0].status == "FAILED"
    assert scans[0].error_code == "SCAN_FAILED"
    assert "report insert failed" in (scans[0].error_message or "")


def test_liquidation_read_failure_becomes_partial_not_silently_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.application.scan_service as scan_service_module
    from oft.ports.store import StoreUnavailableError

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise StoreUnavailableError("liquidation parquet unreadable")

    monkeypatch.setattr(scan_service_module, "read_liquidations", fail)
    harness = _harness(tmp_path)
    try:
        response = _post(harness, key="liquidation-read-failure")
        report_id = response.json()["data"]["report_id"]
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()[
            "data"
        ]
    finally:
        harness.close()

    assert response.status_code == 201
    metrics = report["metrics"]
    assert "liquidations unreadable; treated as MISSING" in metrics["quality_reasons"]


def test_report_path_parameters_are_length_and_charset_bounded(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        too_long = harness.client.get(f"/api/v1/reports/{'A' * 129}", headers=harness.auth)
        bad_chars = harness.client.get("/api/v1/reports/%3Cscript%3E", headers=harness.auth)
    finally:
        harness.close()

    assert too_long.status_code == 422
    assert bad_chars.status_code == 422


def test_csp_includes_object_src_none(tmp_path: Path) -> None:
    harness = _harness(tmp_path, backfill=False)
    try:
        response = harness.client.get("/health")
    finally:
        harness.close()

    assert "object-src 'none'" in response.headers["content-security-policy"]


def test_same_key_concurrent_requests_run_at_most_once(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    harness = _harness(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: _post(harness, key="concurrent-key"), range(2)))
        reports = harness.container.store.list_reports()
    finally:
        harness.close()

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(reports) == 1


def test_api_does_not_expose_absolute_manifest_paths(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        response = _post(harness, key="opaque-paths")
        scan_id = response.json()["data"]["scan"]["scan_id"]
        report_id = response.json()["data"]["report_id"]
        scan = harness.client.get(f"/api/v1/scans/{scan_id}", headers=harness.auth).json()["data"]
        report = harness.client.get(f"/api/v1/reports/{report_id}", headers=harness.auth).json()[
            "data"
        ]
    finally:
        harness.close()

    assert "/" not in scan["manifest_path"]
    for entry in report["metrics_manifest"]:
        assert "/" not in entry["path"]
    assert not report["metrics"]["sources"][0]["ref"].startswith("/")
    assert str(tmp_path) not in report["metrics"]["sources"][0]["ref"]
