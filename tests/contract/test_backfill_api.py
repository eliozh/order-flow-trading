"""Backfill API envelope, estimate, conflict, and CLI exit-code contract."""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.api.envelope import success_envelope
from oft.cli.api_client import ApiClientError
from oft.cli.commands.backfill import exit_code_for_error
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import BackfillRunRecord

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    common = _load("common.schema.json")
    envelope = _load("api-envelope.schema.json")
    resource = Resource.from_contents(common, default_specification=DRAFT202012)
    registry = Registry().with_resource(common["$id"], resource)
    return Draft202012Validator(envelope, registry=registry)


def _validate(validator: Draft202012Validator, payload: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
    )


def _payload(confirm: bool) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "start": "2026-10-01",
        "end": "2026-10-07",
        "data_type": "aggTrades",
        "confirm": confirm,
    }


def test_estimate_returns_before_download(tmp_path: Path, validator: Draft202012Validator) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=_payload(confirm=False), headers=_auth(app))

    assert response.status_code == 200
    body = response.json()
    _validate(validator, body)
    assert body["ok"] is True
    estimate = body["data"]["estimate"]
    assert estimate["files"] == 7
    assert estimate["days"] == 7
    assert estimate["estimated_bytes"] > 0
    assert body["data"]["run"] is None
    assert app.state.container.store.list_backfill_runs() == ()


def test_create_run_and_read_status(tmp_path: Path, validator: Draft202012Validator) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        created = client.post("/api/v1/backfill", json=_payload(confirm=True), headers=_auth(app))
        assert created.status_code == 201
        body = created.json()
        _validate(validator, body)
        run = body["data"]["run"]
        assert run["status"] == "COMPLETE"
        assert run["files_verified"] == 7
        assert len(body["data"]["files"]) == 7

        status = client.get(f"/api/v1/backfill/{run['run_id']}", headers=_auth(app))
        assert status.status_code == 200
        status_body = status.json()
        _validate(validator, status_body)
        assert status_body["data"]["run"]["run_id"] == run["run_id"]
        first = status_body["data"]["files"][0]
        assert {
            "source_url",
            "checksum",
            "bytes",
            "rows",
            "status",
            "verified_at",
            "checksum_verified",
        } <= set(first)
        assert first["status"] == "COMPLETE"
        assert first["checksum_verified"] is True
        assert first["bytes"] > 0
        assert first["rows"] >= 1

        latest = client.get("/api/v1/backfill/latest", headers=_auth(app))
        assert latest.status_code == 200
        _validate(validator, latest.json())
        assert latest.json()["data"]["run"]["run_id"] == run["run_id"]


def test_running_backfill_returns_conflict(tmp_path: Path, validator: Draft202012Validator) -> None:
    app = create_app(_settings(tmp_path))
    app.state.container.store.start_backfill_run(
        BackfillRunRecord(
            run_id="BF-RUNNING",
            symbol="BTCUSDT",
            data_type="aggTrades",
            start_date="2026-10-01",
            end_date="2026-10-07",
            status="RUNNING",
            started_at=utc_timestamp(datetime.now(UTC)),
            finished_at=None,
            estimated_bytes=1,
            processed_bytes=0,
            files_total=7,
            files_verified=0,
            files_failed=0,
            error_code=None,
            message=None,
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=_payload(confirm=True), headers=_auth(app))

    assert response.status_code == 409
    body = response.json()
    _validate(validator, body)
    assert body["error"]["code"] == "BACKFILL_ALREADY_RUNNING"


def test_missing_run_and_invalid_range_use_the_envelope(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        missing = client.get("/api/v1/backfill/BF-MISSING", headers=_auth(app))
        invalid = client.post(
            "/api/v1/backfill",
            json={
                "symbol": "BTCUSDT",
                "start": "2026-10-07",
                "end": "2026-10-01",
                "data_type": "aggTrades",
                "confirm": False,
            },
            headers=_auth(app),
        )

    assert missing.status_code == 404
    _validate(validator, missing.json())
    assert invalid.status_code == 422
    _validate(validator, invalid.json())
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"


# -- CLI ---------------------------------------------------------------------


def _parse_envelope(stdout: str) -> dict[str, Any]:
    """Skip daemon log lines captured because the test daemon shares stdout."""
    marker = '{\n  "ok":'
    index = stdout.find(marker)
    assert index >= 0, stdout
    return json.loads(stdout[index:])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def archive_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    port = _free_port()
    data_dir = tmp_path / "daemon-data"
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(port))
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    monkeypatch.setenv("OFT_ARCHIVE_FIXTURE_DIR", str(ARCHIVE))
    monkeypatch.setenv("OFT_LOG_LEVEL", "WARNING")
    resolved = Settings(
        env="test",
        port=port,
        data_dir=data_dir,
        archive_fixture_dir=ARCHIVE,
        log_level="WARNING",
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(resolved),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("daemon did not start within 10s")
        time.sleep(0.02)
    try:
        yield resolved
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_cli_run_and_status_exit_zero(
    archive_daemon: Settings, validator: Draft202012Validator
) -> None:
    from oft.cli.main import app as cli_app

    runner = CliRunner()
    run_result = runner.invoke(
        cli_app,
        [
            "backfill",
            "run",
            "--symbol",
            "BTCUSDT",
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-07",
            "--yes",
            "--json",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    envelope = _parse_envelope(run_result.stdout)
    _validate(validator, envelope)
    run_id = envelope["data"]["run"]["run_id"]

    status_result = runner.invoke(cli_app, ["backfill", "status", run_id, "--json"])
    assert status_result.exit_code == 0, status_result.output
    status_envelope = _parse_envelope(status_result.stdout)
    _validate(validator, status_envelope)
    assert status_envelope["data"]["run"]["files_verified"] == 7
    assert len(status_envelope["data"]["files"]) == 7


class _FakeApiClient:
    result: dict[str, Any] | None = None
    error: Exception | None = None

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> _FakeApiClient:
        return cls()

    def create_backfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def get_backfill(self, run_id: str) -> dict[str, Any]:
        return self.create_backfill({})


def _fake_envelope(status: str) -> dict[str, Any]:
    return success_envelope(
        {
            "estimate": {"files": 7},
            "run": {"run_id": "BF-TEST", "status": status},
            "files": [],
        },
        request_id="REQ-TEST",
    )


def test_cli_partial_and_blocked_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oft.cli.commands.backfill as backfill_cli
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(backfill_cli, "ApiClient", _FakeApiClient)
    runner = CliRunner()

    _FakeApiClient.result = _fake_envelope("PARTIAL")
    _FakeApiClient.error = None
    partial = runner.invoke(
        cli_app,
        [
            "backfill",
            "run",
            "--symbol",
            "BTCUSDT",
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-07",
            "--yes",
        ],
    )
    assert partial.exit_code == 3, partial.output

    _FakeApiClient.error = ApiClientError("BACKFILL_ALREADY_RUNNING", "already running")
    blocked = runner.invoke(
        cli_app,
        [
            "backfill",
            "run",
            "--symbol",
            "BTCUSDT",
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-07",
            "--yes",
        ],
    )
    assert blocked.exit_code == 4, blocked.output


def test_cli_config_error_exits_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oft.cli.main import app as cli_app

    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OFT_LOG_LEVEL", "BOGUS")
    result = CliRunner().invoke(
        cli_app,
        ["backfill", "status", "--json"],
    )

    assert result.exit_code == 5, result.output


def test_data_quality_endpoint_reports_complete_then_partial(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    params = {"symbol": "BTCUSDT", "start": "2026-10-01", "end": "2026-10-07"}

    with TestClient(app) as client:
        created = client.post("/api/v1/backfill", json=_payload(confirm=True), headers=_auth(app))
        assert created.status_code == 201, created.text

        complete = client.get("/api/v1/data/quality", params=params, headers=_auth(app))
        assert complete.status_code == 200, complete.text
        body = complete.json()
        _validate(validator, body)
        assert body["data"]["status"] == "COMPLETE"
        assert body["data"]["expected_days"] == 7
        assert body["data"]["present_days"] == 7
        assert body["data"]["gaps"] == []
        assert body["meta"]["data_quality"] == "COMPLETE"

        shutil.rmtree(
            settings.data_dir
            / "market"
            / "um"
            / "agg_trades"
            / "symbol=BTCUSDT"
            / "date=2026-10-04"
        )
        partial = client.get("/api/v1/data/quality", params=params, headers=_auth(app))
        assert partial.status_code == 200, partial.text
        partial_body = partial.json()
        _validate(validator, partial_body)
        assert partial_body["data"]["status"] == "PARTIAL"
        assert partial_body["data"]["present_days"] == 6
        assert len(partial_body["data"]["gaps"]) == 1
        assert partial_body["data"]["gaps"][0]["reason"] == "ARCHIVE_FILE_MISSING"
        assert partial_body["meta"]["data_quality"] == "PARTIAL"

        bad_range = client.get(
            "/api/v1/data/quality",
            params={"symbol": "BTCUSDT", "start": "2026-10-07", "end": "2026-10-01"},
            headers=_auth(app),
        )
        assert bad_range.status_code == 422
        assert bad_range.json()["error"]["code"] == "VALIDATION_FAILED"


def test_backfill_rejects_a_symbol_outside_the_registry(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    app = create_app(_settings(tmp_path))
    payload = _payload(confirm=False)
    payload["symbol"] = "ZZZUSDT"

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=payload, headers=_auth(app))

    assert response.status_code == 422
    body = response.json()
    _validate(validator, body)
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "symbol registry" in body["error"]["message"]


def test_resolved_gaps_do_not_degrade_backfill_or_data_quality(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    from oft.domain.data_quality import DataGap, GapSeverity

    app = create_app(_settings(tmp_path))
    params = {"symbol": "BTCUSDT", "start": "2026-10-01", "end": "2026-10-07"}
    with TestClient(app) as client:
        created = client.post("/api/v1/backfill", json=_payload(confirm=True), headers=_auth(app))
        assert created.status_code == 201, created.text
        run = created.json()["data"]["run"]
        app.state.container.store.upsert_data_gap(
            DataGap(
                gap_id="GAP-BTCUSDT-aggTrades-2026-10-04",
                symbol="BTCUSDT",
                data_type="aggTrades",
                start="2026-10-04T00:00:00.000Z",
                end="2026-10-04T23:59:59.999Z",
                reason="ARCHIVE_FILE_MISSING",
                severity=GapSeverity.CRITICAL,
                detected_at="2026-11-06T12:00:00.000Z",
                source="BACKFILL",
                resolved_at="2026-11-06T13:00:00.000Z",
                resolution="BACKFILL_RESOLVED",
                run_id=run["run_id"],
            )
        )
        status = client.get(f"/api/v1/backfill/{run['run_id']}", headers=_auth(app))
        quality = client.get("/api/v1/data/quality", params=params, headers=_auth(app))

    _validate(validator, status.json())
    _validate(validator, quality.json())
    assert status.json()["meta"]["data_quality"] == "COMPLETE"
    assert status.json()["data"]["run"]["status"] == "COMPLETE"
    assert quality.json()["meta"]["data_quality"] == "COMPLETE"
    assert quality.json()["data"]["status"] == "COMPLETE"


def test_runtime_error_exit_codes_match_adr_0003() -> None:
    assert exit_code_for_error("DATA_UNAVAILABLE") == 1
    assert exit_code_for_error("INTERNAL_ERROR") == 1
    assert exit_code_for_error("VALIDATION_FAILED") == 2
    assert exit_code_for_error("BACKFILL_ALREADY_RUNNING") == 4
    assert exit_code_for_error("CONFIG_INVALID") == 5


def test_data_quality_rejects_unsupported_data_type(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/data/quality",
            params={"symbol": "BTCUSDT", "data_type": "klines"},
            headers=_auth(app),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_data_quality_probe_does_not_persist_gaps(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    params = {"symbol": "BTCUSDT", "start": "2026-10-01", "end": "2026-10-07"}

    with TestClient(app) as client:
        response = client.get("/api/v1/data/quality", params=params, headers=_auth(app))

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "PARTIAL"
    assert len(body["data"]["gaps"]) == 7
    assert body["data"]["detected_at"]
    assert body["data"]["run_id"] is None
    assert app.state.container.store.list_data_gaps(symbol="BTCUSDT") == ()


def test_estimate_is_stamped_estimated_not_complete(
    tmp_path: Path, validator: Draft202012Validator
) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=_payload(confirm=False), headers=_auth(app))

    body = response.json()
    _validate(validator, body)
    assert body["meta"]["data_quality"] == "ESTIMATED"


def test_unsupported_data_type_fails_before_creating_a_run(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    payload = _payload(confirm=True)
    payload["data_type"] = "trades"

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=payload, headers=_auth(app))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_backfill_runs() == ()


def test_test_environment_without_a_fixture_never_uses_https(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post("/api/v1/backfill", json=_payload(confirm=False), headers=_auth(app))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"
