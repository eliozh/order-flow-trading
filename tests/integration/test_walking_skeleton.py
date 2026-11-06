"""Walking-skeleton integration test: fixture -> store -> delta -> API -> CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.application.fixture_scan import run_fixture_scan
from oft.bootstrap import build_container, get_status
from oft.cli.main import app as cli_app
from oft.config import Settings

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "market" / "btcusdt_agg-trades_sample.csv"
)


def _configure_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _create_scan(client: TestClient, app: Any, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fixture_path": str(FIXTURE),
        "symbol": "BTCUSDT",
        "period": "4h",
        "metrics_only": True,
    }
    payload.update(overrides)
    response = client.post("/api/v1/scans", json=payload, headers=_auth(app))
    assert response.status_code == 201, response.text
    return response.json()


def test_status_envelope_matches_domain_report(settings: Settings) -> None:
    app = create_app(settings)
    expected = get_status(settings)

    with TestClient(app) as client:
        health = client.get("/health")
        response = client.get("/api/v1/status", headers=_auth(app))

    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["data"]["status"] == "ok"
    assert health.json()["data"]["version"] == expected.version

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["env"] == "test"
    assert body["data"]["port"] == 8765
    assert body["data"]["universe"] == expected.universe.model_dump(mode="json")
    assert body["meta"]["provenance"] == "CALC"
    assert body["meta"]["data_quality"] == "COMPLETE"
    assert body["meta"]["generated_at"] == body["data"]["generated_at"]
    assert body["data"]["generated_at"].endswith("Z")


def test_unknown_route_uses_error_envelope(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/not-a-route", headers=_auth(app))

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["http_status"] == 404


def test_fixture_flows_through_parquet_manifest_and_delta(settings: Settings) -> None:
    container = build_container(settings, initialize=True)

    result = run_fixture_scan(
        container,
        fixture_path=FIXTURE,
        symbol="BTCUSDT",
        period="4h",
        metrics_only=True,
    )

    assert result.scan_id.startswith("SCAN-")
    assert result.delta == "1.50"
    assert result.buy_volume == "4.25"
    assert result.sell_volume == "2.75"
    assert result.cvd == "1.50"
    assert result.trade_count == 6
    assert result.status == "DONE"
    assert result.data_quality == "COMPLETE"

    part = Path(result.manifest_path)
    assert part.exists()
    assert part.parent.parent.parent.name == "agg_trades"
    assert part.parent.name.startswith("date=")
    assert "symbol=BTCUSDT" in part.parts

    import pyarrow.parquet as pq

    table = pq.read_table(part)
    assert table.num_rows == 6
    assert table.column("is_buyer_maker").to_pylist() == [
        False,
        True,
        False,
        True,
        False,
        True,
    ]

    store = container.store
    assert isinstance(store, SqliteStore)
    manifest = store.list_manifest()
    assert len(manifest) == 1
    assert manifest[0].path == result.manifest_path
    assert manifest[0].rows == 6
    assert manifest[0].data_type == "agg_trades"

    parquet = container.parquet
    assert isinstance(parquet, ParquetStore)
    assert part.is_relative_to(settings.data_dir)


def test_api_scan_round_trip_and_readiness_match_golden(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        created = _create_scan(client, app)
        data = created["data"]
        assert data["delta"] == "1.50"
        assert data["buy_volume"] == "4.25"
        assert data["sell_volume"] == "2.75"
        assert data["cvd"] == "1.50"
        assert data["trade_count"] == 6

        fetched = client.get(f"/api/v1/scans/{data['scan_id']}", headers=_auth(app))
        assert fetched.status_code == 200
        assert fetched.json()["data"] == data

        ready = client.get("/ready")
        assert ready.status_code == 200
        ready_data = ready.json()["data"]
        assert ready_data["latest_scan"]["scan_id"] == data["scan_id"]
        assert ready_data["latest_scan"]["delta"] == "1.50"


def test_cli_scan_run_and_status_match_api_value(live_daemon: Settings) -> None:
    from oft.cli.api_client import ApiClient

    runner = CliRunner()
    run_result = runner.invoke(
        cli_app,
        [
            "scan",
            "run",
            "--fixture",
            str(FIXTURE),
            "--symbol",
            "BTCUSDT",
            "--period",
            "4h",
            "--metrics-only",
            "--json",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.stdout)
    assert run_payload["ok"] is True
    run_data = run_payload["data"]
    assert run_data["delta"] == "1.50"
    assert run_data["cvd"] == "1.50"

    api_data = ApiClient.from_settings(live_daemon).get_scan(run_data["scan_id"])["data"]
    assert api_data["delta"] == run_data["delta"]
    assert api_data["cvd"] == run_data["cvd"]

    status_result = runner.invoke(cli_app, ["scan", "status", run_data["scan_id"], "--json"])
    assert status_result.exit_code == 0, status_result.output
    status_payload = json.loads(status_result.stdout)
    assert status_payload["ok"] is True
    assert status_payload["data"]["delta"] == api_data["delta"]
    assert status_payload["data"]["scan_id"] == api_data["scan_id"]


def test_scan_run_without_daemon_reports_daemon_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_environment(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_app,
        [
            "scan",
            "run",
            "--fixture",
            str(FIXTURE),
            "--symbol",
            "BTCUSDT",
            "--period",
            "4h",
            "--metrics-only",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DAEMON_NOT_RUNNING"
    # The CLI must never write when the daemon is the only writer.
    assert not (settings.data_dir / "oft.db").exists()
    assert not (settings.data_dir / "market").exists()


def test_scan_status_without_daemon_reports_daemon_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_environment(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_app, ["scan", "status", "SCAN-ANY", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "DAEMON_NOT_RUNNING"


def test_cli_status_and_doctor_work_without_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_environment(monkeypatch, tmp_path)
    runner = CliRunner()

    status_result = runner.invoke(cli_app, ["status"])
    assert status_result.exit_code == 0, status_result.output
    assert "OFFLINE" in status_result.stdout
    assert "Environment" in status_result.stdout
    assert "BTCUSDT" in status_result.stdout
    assert "TRADIFI_PERPETUAL" in status_result.stdout

    status_json = runner.invoke(cli_app, ["status", "--json"])
    assert status_json.exit_code == 0, status_json.output
    payload = json.loads(status_json.stdout)
    assert payload["ok"] is True
    assert payload["data"]["daemon"]["state"] == "OFFLINE"
    assert payload["data"]["last_scan"] is None

    doctor_result = runner.invoke(cli_app, ["doctor"])
    assert doctor_result.exit_code == 0, doctor_result.output
    assert "DEGRADED" in doctor_result.stdout
    assert "storage.sqlite" in doctor_result.stdout
    assert "market_data" in doctor_result.stdout

    doctor_json = runner.invoke(cli_app, ["doctor", "--json"])
    assert doctor_json.exit_code == 0, doctor_json.output
    doctor_payload = json.loads(doctor_json.stdout)
    assert doctor_payload["data"]["status"] == "DEGRADED"
    names = {check["name"] for check in doctor_payload["data"]["checks"]}
    assert {"config", "storage.sqlite", "storage.parquet", "market_data", "daemon"} <= names


def test_status_reads_sqlite_read_only_after_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure_environment(monkeypatch, tmp_path)
    container = build_container(settings, initialize=True)
    result = run_fixture_scan(
        container,
        fixture_path=FIXTURE,
        symbol="BTCUSDT",
        period="4h",
        metrics_only=True,
    )

    runner = CliRunner()
    status_result = runner.invoke(cli_app, ["status", "--json"])
    assert status_result.exit_code == 0, status_result.output
    payload = json.loads(status_result.stdout)
    assert payload["data"]["daemon"]["state"] == "OFFLINE"
    assert payload["data"]["last_scan"]["scan_id"] == result.scan_id
    assert payload["data"]["last_scan"]["delta"] == "1.50"


def test_scan_status_reports_not_found(live_daemon: Settings) -> None:
    runner = CliRunner()

    result = runner.invoke(cli_app, ["scan", "status", "SCAN-DOES-NOT-EXIST", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


def test_api_requires_bearer_token(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        status_response = client.get("/api/v1/status")
        scan_response = client.get("/api/v1/scans/SCAN-NONE")

    assert status_response.status_code == 401
    assert status_response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert scan_response.status_code == 401
    assert scan_response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_mutation_rejects_foreign_origin(settings: Settings) -> None:
    app = create_app(settings)
    payload = {
        "fixture_path": str(FIXTURE),
        "symbol": "BTCUSDT",
        "period": "4h",
        "metrics_only": True,
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scans",
            json=payload,
            headers={**_auth(app), "Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"


def test_browser_mutation_requires_csrf_token(settings: Settings) -> None:
    app = create_app(settings)
    origin = f"http://127.0.0.1:{settings.port}"
    payload = {
        "fixture_path": str(FIXTURE),
        "symbol": "BTCUSDT",
        "period": "4h",
        "metrics_only": True,
    }

    with TestClient(app) as client:
        client.get("/health")
        csrf_token = client.cookies.get("oft_csrf")
        assert csrf_token

        rejected = client.post(
            "/api/v1/scans",
            json=payload,
            headers={**_auth(app), "Origin": origin},
        )
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "CSRF_INVALID"

        accepted = client.post(
            "/api/v1/scans",
            json=payload,
            headers={
                **_auth(app),
                "Origin": origin,
                "X-CSRF-Token": csrf_token,
            },
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["data"]["delta"] == "1.50"


def test_invalid_scan_request_is_validation_failed(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scans",
            json={"fixture_path": str(FIXTURE)},
            headers=_auth(app),
        )

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"]


def test_missing_fixture_is_data_unavailable(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scans",
            json={
                "fixture_path": str(FIXTURE.parent / "missing.csv"),
                "symbol": "BTCUSDT",
                "period": "4h",
                "metrics_only": True,
            },
            headers=_auth(app),
        )

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "DATA_UNAVAILABLE"


def test_scan_rejects_fixture_path_traversal(settings: Settings) -> None:
    app = create_app(settings)
    traversal = f"{FIXTURE.parent}/../../../pyproject.toml"

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scans",
            json={
                "fixture_path": traversal,
                "symbol": "BTCUSDT",
                "period": "4h",
                "metrics_only": True,
            },
            headers=_auth(app),
        )

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_FAILED"


_READINESS_FAILURE_CODES = [
    ("config", "CONFIG_INVALID"),
    ("storage.sqlite", "STORAGE_UNAVAILABLE"),
    ("storage.parquet", "STORAGE_UNAVAILABLE"),
    ("clock", "CLOCK_DRIFT"),
    ("market_data", "DATA_UNAVAILABLE"),
]


@pytest.mark.parametrize(("failed_check", "expected_code"), _READINESS_FAILURE_CODES)
def test_ready_labels_each_failed_check_group(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failed_check: str,
    expected_code: str,
) -> None:
    from oft.api.errors import ERROR_CATALOG
    from oft.application.health import CheckResult, ReadinessReport

    def fake_readiness(
        resolved: Settings, store: Any, market_data: Any, clock: Any
    ) -> ReadinessReport:
        names = ["config", "storage.sqlite", "storage.parquet", "clock", "market_data"]
        checks = tuple(
            CheckResult(
                name=name,
                status="degraded" if name == failed_check else "ok",
                detail="forced failure" if name == failed_check else "ok",
            )
            for name in names
        )
        return ReadinessReport(
            status="not_ready",
            version="0.1.0",
            env=resolved.env.value,
            data_dir=str(resolved.data_dir),
            checked_at="2026-11-06T00:00:00.000Z",
            checks=checks,
            latest_scan=None,
        )

    monkeypatch.setattr("oft.api.routes.system.build_readiness", fake_readiness)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == ERROR_CATALOG[expected_code].http_status
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == expected_code


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return dist


def test_security_headers_on_health_and_system(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oft.api.app._web_dist", lambda: _fake_dist(tmp_path))
    app = create_app(settings)

    with TestClient(app) as client:
        for path in ("/health", "/system"):
            response = client.get(path)
            assert response.status_code == 200
            headers = response.headers
            assert headers["x-frame-options"] == "DENY"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["referrer-policy"] == "no-referrer"
            csp = headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "script-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "base-uri 'self'" in csp


def test_unknown_static_asset_returns_404_while_spa_routes_still_serve(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oft.api.app._web_dist", lambda: _fake_dist(tmp_path))
    app = create_app(settings)

    with TestClient(app) as client:
        missing = client.get("/favicon.ico")
        spa = client.get("/system")
        asset = client.get("/assets/app.js")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert spa.status_code == 200
    assert 'id="root"' in spa.text
    assert asset.status_code == 200


def test_log_redaction_masks_bearer_tokens(caplog: pytest.LogCaptureFixture) -> None:
    from oft.logging import configure_logging, redact_secrets

    configure_logging(Settings(env="test", api_token="super-secret"))

    event = redact_secrets(
        None,
        "info",
        {"event": "request", "authorization": "Bearer super-secret", "note": "Bearer abc123"},
    )

    assert event["authorization"] == "[REDACTED]"
    assert "abc123" not in str(event)


def test_foreign_host_header_is_host_forbidden(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        rebound = client.get("/health", headers={"Host": "attacker.example"})
        wrong_port = client.get("/health", headers={"Host": f"127.0.0.1:{settings.port + 1}"})

    assert rebound.status_code == 403
    assert rebound.json()["error"]["code"] == "HOST_FORBIDDEN"
    assert wrong_port.status_code == 403
    assert wrong_port.json()["error"]["code"] == "HOST_FORBIDDEN"


def test_malformed_host_port_is_host_forbidden(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": "127.0.0.1:notaport"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "HOST_FORBIDDEN"


def test_configured_non_loopback_bind_host_is_allowed(tmp_path: Path) -> None:
    settings = Settings(env="test", host="0.0.0.0", port=8766, data_dir=tmp_path / "data")
    app = create_app(settings)

    with TestClient(app) as client:
        allowed = client.get("/health", headers={"Host": "0.0.0.0:8766"})
        foreign = client.get("/health", headers={"Host": "evil.example:8766"})

    assert allowed.status_code == 200
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "HOST_FORBIDDEN"


def test_ready_reports_the_configured_environment(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["data"]["env"] == "test"


def test_api_client_brackets_ipv6_host(live_ipv6_daemon: Settings) -> None:
    from oft.cli.api_client import ApiClient

    envelope = ApiClient.from_settings(live_ipv6_daemon)._request("GET", "/ready")

    assert envelope["ok"] is True
    assert envelope["data"]["status"] == "ready"
    assert envelope["data"]["env"] == "test"


def test_loopback_host_with_configured_port_is_allowed(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": f"127.0.0.1:{settings.port}"})

    assert response.status_code == 200


def test_scan_rejects_when_another_scan_is_in_flight(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        acquired = app.state.scan_lock.acquire(blocking=False)
        assert acquired
        try:
            response = client.post(
                "/api/v1/scans",
                json={
                    "fixture_path": str(FIXTURE),
                    "symbol": "BTCUSDT",
                    "period": "4h",
                    "metrics_only": True,
                },
                headers=_auth(app),
            )
        finally:
            app.state.scan_lock.release()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_ALREADY_RUNNING"


def test_manifest_failure_cleans_up_orphan_part(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    container = build_container(settings, initialize=True)

    def fail(record: Any, entry: Any) -> None:
        raise sqlite3.OperationalError("simulated manifest write failure")

    monkeypatch.setattr(container.store, "save_scan_with_manifest", fail)

    with pytest.raises(sqlite3.OperationalError):
        run_fixture_scan(
            container,
            fixture_path=FIXTURE,
            symbol="BTCUSDT",
            period="4h",
            metrics_only=True,
        )

    parts = list((settings.data_dir / "market" / "um" / "agg_trades").rglob("*.parquet"))
    assert parts == []
    assert container.store.list_manifest() == ()
    assert container.store.latest_scan() is None


def test_sequential_scans_publish_unique_parts(settings: Settings) -> None:
    container = build_container(settings, initialize=True)

    first = run_fixture_scan(
        container, fixture_path=FIXTURE, symbol="BTCUSDT", period="4h", metrics_only=True
    )
    second = run_fixture_scan(
        container, fixture_path=FIXTURE, symbol="BTCUSDT", period="4h", metrics_only=True
    )

    assert first.manifest_path != second.manifest_path
    assert Path(first.manifest_path).is_file()
    assert Path(second.manifest_path).is_file()
    assert len(container.store.list_manifest()) == 2
