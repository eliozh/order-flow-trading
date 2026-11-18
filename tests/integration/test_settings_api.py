"""``/api/v1/settings`` and ``oft config`` integration tests (slice 32)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore, database_path
from oft.api.app import create_app
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings, load_settings
from oft.domain.risk import RISK_CAPS_STATE_KEY
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
SECRET_MARKERS = (
    "api_key",
    "api_secret",
    "token",
    "password",
    "secret",
    "openai_api_key",
    "anthropic_api_key",
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


@pytest.fixture(autouse=True)
def _clear_scan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every settings test starts without an ambient scan cadence."""
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put(
    client: TestClient,
    auth: dict[str, str],
    payload: dict[str, Any],
    *,
    key: str = "settings-key-1",
) -> Any:
    return client.put(
        "/api/v1/settings/scan",
        json=payload,
        headers={**auth, "Idempotency-Key": key},
    )


def test_get_scan_settings_defaults_to_the_environment(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=auth)

        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["ok"] is True
        data = envelope["data"]
        assert data["key"] == "scan.period"
        assert data["value"] == "4h"
        assert data["source"] == "default"
        assert data["supported"] == ["4h", "8h"]
        assert data["timezone"] == "UTC"
        assert data["next_run"] == "2026-11-06T12:00:00.000Z"
        assert data["next_run"].endswith("Z")
        assert data["updated_at"] is None
        assert data["updated_by"] is None
        assert envelope["meta"]["provenance"] == "CALC"
        assert envelope["meta"]["data_quality"] == "COMPLETE"
        assert not any(marker in json.dumps(data) for marker in SECRET_MARKERS)

        # No persisted row exists, so the environment remains the source.
        assert app.state.container.store.list_config() == ()


def test_get_scan_settings_reads_the_environment_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFT_SCAN_PERIOD", "8h")
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=_auth(app))

    data = response.json()["data"]
    assert data["value"] == "8h"
    assert data["source"] == "default"
    assert data["next_run"] == "2026-11-06T16:00:00.000Z"


@pytest.mark.parametrize("value_json", ["not-json", json.dumps("1h")])
def test_corrupt_persisted_period_is_config_invalid_and_never_guessed(
    tmp_path: Path, value_json: str
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json=value_json,
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=_auth(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_persisted_value_beats_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFT_SCAN_PERIOD", "8h")
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json=json.dumps("4h"),
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        data = client.get("/api/v1/settings/scan", headers=_auth(app)).json()["data"]

    assert data["value"] == "4h"
    assert data["source"] == "user"


def test_pre_v26_database_without_a_config_table_reads_as_no_persisted_row(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    # A zero-byte data directory is the normal state after an upgrade but
    # before the daemon/serve first initializes the v26 schema.
    database_path(settings).write_bytes(b"")

    store = SqliteStore(settings)

    assert store.get_config("scan.period") is None
    assert store.list_config() == ()


def test_missing_database_is_read_only_and_has_no_config(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings)

    assert store.get_config("scan.period") is None
    assert store.list_config() == ()
    # Diagnostics must never create the database.
    assert not (settings.data_dir / "oft.db").exists()


def test_put_with_an_extra_field_is_validation_failed_and_writes_nothing(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"period": "8h", "extra": 1})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_put_persists_one_row_and_switches_the_source_to_user(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put(client, auth, {"period": "8h"})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["value"] == "8h"
        assert data["source"] == "user"
        assert data["updated_at"] == "2026-11-06T10:00:00.000Z"
        assert data["updated_by"] == "web"

        rows = app.state.container.store.list_config()
        assert len(rows) == 1
        assert rows[0].key == "scan.period"
        assert json.loads(rows[0].value_json) == "8h"

        fetched = client.get("/api/v1/settings/scan", headers=auth).json()["data"]
        assert fetched["value"] == "8h"
        assert fetched["source"] == "user"


def test_repeated_identical_put_replays_the_original_envelope(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"period": "8h"}, key="stable-key")
        second = _put(client, auth, {"period": "8h"}, key="stable-key")

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["meta"]["idempotent_replay"] is True
        assert second.json()["data"] == first.json()["data"]
        assert len(app.state.container.store.list_config()) == 1
        # M16/F5: an identical replay never writes a second audit event.
        audits = app.state.container.store.list_audit_events()
        assert len(audits) == 1
        assert audits[0].event_type == "CONFIG_UPDATED"


def test_a_fresh_key_saving_the_effective_value_writes_nothing(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"period": "8h"}, key="noop-first")
        assert first.status_code == 200, first.text
        before_audits = app.state.container.store.list_audit_events()

        # The effective value is already 8h; a different key is a new logical
        # save and must be a deterministic no-op: no row, no audit, 200.
        second = _put(client, auth, {"period": "8h"}, key="noop-second")
        assert second.status_code == 200, second.text
        assert second.json()["data"]["value"] == "8h"
        assert second.json()["data"]["source"] == "user"
        assert second.json()["meta"]["idempotent_replay"] is False

        assert len(app.state.container.store.list_config()) == 1
        assert app.state.container.store.list_audit_events() == before_audits


def test_get_scan_settings_with_an_invalid_environment_period_is_config_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=_auth(app))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_status_schedule_overlays_the_persisted_user_period(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json=json.dumps("8h"),
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/status", headers=_auth(app))

    assert response.status_code == 200, response.text
    schedule = response.json()["data"]["schedule"]
    assert schedule["period"] == "8h"
    assert schedule["pending_period"] is None
    # The 8h window's fire time carries the deterministic 30 s jitter.
    assert schedule["next_run_at"].startswith("2026-11-06T16:00:")


def test_status_drops_the_schedule_and_logs_when_the_row_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application import status as status_module

    warnings: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def warning(self, event: str, **fields: Any) -> None:
            warnings.append((event, fields))

    # A module-level recorder is order-independent: structlog's
    # ``cache_logger_on_first_use`` can otherwise hide the event after another
    # test has run ``configure_logging``.
    monkeypatch.setattr(status_module, "_LOGGER", _Recorder())

    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json="not-json",
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/status", headers=_auth(app))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["schedule"] is None
    assert len(warnings) == 1
    event, fields = warnings[0]
    assert event == "schedule_status_unavailable"
    assert "persisted scan.period" in str(fields.get("error", ""))


def test_a_different_value_put_over_a_corrupt_row_is_config_invalid_with_no_write(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    corrupt = ConfigRecord(
        key="scan.period",
        value_json="not-json",
        updated_at="2026-11-06T09:00:00.000Z",
        updated_by="web",
    )
    store.set_config(corrupt)

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"period": "8h"}, key="corrupt-save")

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    row = store.get_config("scan.period")
    assert row is not None
    assert row.value_json == "not-json"
    assert row.updated_at == corrupt.updated_at
    assert store.list_audit_events() == ()


def test_same_key_with_a_different_payload_is_an_idempotency_conflict(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"period": "8h"}, key="conflict-key")
        assert first.status_code == 200, first.text
        second = _put(client, auth, {"period": "4h"}, key="conflict-key")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_put_requires_an_idempotency_key(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/scan",
            json={"period": "8h"},
            headers=_auth(app),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert app.state.container.store.list_config() == ()


@pytest.mark.parametrize("period", ["1h", "3h", "eight", "24h", ""])
def test_invalid_periods_return_validation_failed_and_write_nothing(
    tmp_path: Path, period: str
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"period": period})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"][0]["field"] == "period"
    assert app.state.container.store.list_config() == ()
    assert app.state.container.store.list_audit_events() == ()


def test_non_string_period_returns_validation_failed(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"period": 8})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"][0]["field"] == "period"
    assert app.state.container.store.list_config() == ()


def test_malformed_json_is_a_validation_failure(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings/scan",
            content='{"period": "8h"',
            headers={
                **_auth(app),
                "Idempotency-Key": "malformed",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert app.state.container.store.list_config() == ()
    # A malformed request body writes no audit event either.
    assert app.state.container.store.list_audit_events() == ()


def test_settings_sections_expose_source_and_keys_without_values(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        before = client.get("/api/v1/settings", headers=auth)
        assert before.status_code == 200, before.text
        section = before.json()["data"]["sections"]
        assert len(section) == 3
        assert section[0] == {
            "section": "scan",
            "source": "default",
            "last_changed": None,
            "keys": ["scan.period"],
        }
        assert section[1] == {
            "section": "risk",
            "source": "default",
            "last_changed": None,
            "keys": [
                "per_trade_risk_pct",
                "max_concurrent_positions",
                "daily_loss_pct",
                "max_leverage",
                "max_notional_pct",
                "max_slippage_bps",
                "max_spread_bps",
                "stale_mark_seconds",
            ],
        }
        assert "value" not in json.dumps(section)

        _put(client, auth, {"period": "8h"})
        after = client.get("/api/v1/settings", headers=auth).json()["data"]["sections"][0]
        assert after["source"] == "user"
        assert after["last_changed"] == "2026-11-06T10:00:00.000Z"


def test_settings_sections_are_metadata_only_for_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overview must not build the utilization snapshot."""
    from oft.application.settings_service import SettingsService

    def _boom(self: Any, caps: Any) -> Any:
        raise AssertionError("read_sections must not compute utilization")

    monkeypatch.setattr(SettingsService, "_utilization", _boom)
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get("/api/v1/settings", headers=_auth(app))

    assert response.status_code == 200, response.text
    assert [section["section"] for section in response.json()["data"]["sections"]] == [
        "scan",
        "risk",
        "telegram",
    ]
    # The detail route still owns the snapshot and is expected to fail here.
    with TestClient(app) as client, pytest.raises(AssertionError):
        client.get("/api/v1/settings/risk", headers=_auth(app))


def test_corrupt_persisted_risk_caps_on_the_overview_map_to_config_invalid(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    app.state.container.store.set_system_state(
        RISK_CAPS_STATE_KEY, "not-json", updated_at="2026-11-06T10:00:00.000Z"
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/settings", headers=_auth(app))

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_settings_spa_entries_establish_a_browser_session(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        # The auth prompt (401 HTML) proves the path is a recognized SPA entry,
        # not an API miss.
        assert client.get("/settings").status_code == 401
        assert client.get("/settings/").status_code == 401
        assert client.get("/settings/scan").status_code == 401


def test_api_and_cli_sources_never_import_sqlite3() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "oft"
    for package in ("api", "cli"):
        for path in (root / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "sqlite3" not in source, path


# -- ``oft config`` CLI ------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("OFT_SCAN_PERIOD", raising=False)
    monkeypatch.delenv("OFT_SCAN_JITTER_SECONDS", raising=False)


def _seed_persisted_period(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    """A valid saved ``scan.period`` in the CLI data dir (no schedule.json)."""
    _cli_env(monkeypatch, tmp_path)
    store = SqliteStore(load_settings())
    store.initialize()
    store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json=json.dumps(value),
            updated_at="2026-11-06T09:00:00.000Z",
            updated_by="web",
        )
    )


def test_cli_get_set_show_and_validate_match_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    get_default = runner.invoke(cli_app, ["config", "get", "scan.period"])
    assert get_default.exit_code == 0, get_default.output
    assert get_default.output.strip() == "4h"

    set_value = runner.invoke(cli_app, ["config", "set", "scan.period", "8h"])
    assert set_value.exit_code == 0, set_value.output

    get_saved = runner.invoke(cli_app, ["config", "get", "scan.period"])
    assert get_saved.exit_code == 0, get_saved.output
    assert get_saved.output.strip() == "8h"

    shown = runner.invoke(cli_app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["key"] == "scan.period"
    assert data["value"] == "8h"
    assert data["source"] == "user"
    assert data["supported"] == ["4h", "8h"]
    # CLI/API parity includes the next UTC boundary for the effective period.
    assert data["next_run"].endswith("Z")
    next_run = datetime.fromisoformat(data["next_run"].replace("Z", "+00:00"))
    assert next_run.hour in (0, 8, 16)
    assert next_run.minute == 0 and next_run.second == 0

    validated = runner.invoke(cli_app, ["config", "validate", "--json"])
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["data"]["status"] == "PASS"


def test_cli_and_api_agree_on_value_source_and_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8 parity: the same data dir and clock through both real entry points."""
    _cli_env(monkeypatch, tmp_path)
    from oft.bootstrap import build_container
    from oft.cli.commands import config as config_module

    def _fixed_container(settings: Any, *, initialize: bool = False) -> Any:
        return build_container(settings, initialize=initialize, clock=FixedClock(NOW))

    monkeypatch.setattr(config_module, "build_container", _fixed_container)
    runner = CliRunner()

    saved = runner.invoke(cli_app, ["config", "set", "scan.period", "8h"])
    assert saved.exit_code == 0, saved.output
    shown = runner.invoke(cli_app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    cli_data = json.loads(shown.output)["data"]
    cli_get = runner.invoke(cli_app, ["config", "get", "scan.period"])
    assert cli_get.exit_code == 0, cli_get.output

    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    with TestClient(app) as client:
        response = client.get("/api/v1/settings/scan", headers=_auth(app))
    assert response.status_code == 200, response.text
    api_data = response.json()["data"]

    assert cli_get.output.strip() == api_data["value"] == "8h"
    for field in (
        "key",
        "value",
        "source",
        "supported",
        "next_run",
        "timezone",
        "updated_at",
        "updated_by",
    ):
        assert cli_data[field] == api_data[field], field
    assert api_data["next_run"] == "2026-11-06T16:00:00.000Z"


def test_cli_set_rejects_an_invalid_period_with_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_app, ["config", "set", "scan.period", "3h"])

    assert result.exit_code == 1
    get_after = runner.invoke(cli_app, ["config", "get", "scan.period"])
    assert get_after.exit_code == 0
    assert get_after.output.strip() == "4h"


def test_cli_invalid_environment_period_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")
    runner = CliRunner()

    result = runner.invoke(cli_app, ["config", "get", "scan.period"])

    assert result.exit_code == 5


def test_cli_get_rejects_an_unknown_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_app, ["config", "get", "risk.per_trade"])

    assert result.exit_code == 1


def test_cli_text_and_json_outputs_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    shown = runner.invoke(cli_app, ["config", "show"])
    assert shown.exit_code == 0, shown.output
    assert "scan.period" in shown.output
    assert "Source" in shown.output
    assert "4h" in shown.output

    get_json = runner.invoke(cli_app, ["config", "get", "scan.period", "--json"])
    assert get_json.exit_code == 0, get_json.output
    assert json.loads(get_json.output)["data"] == {
        "key": "scan.period",
        "value": "4h",
        "source": "default",
    }

    set_json = runner.invoke(cli_app, ["config", "set", "scan.period", "8h", "--json"])
    assert set_json.exit_code == 0, set_json.output
    assert json.loads(set_json.output)["data"]["value"] == "8h"

    noop = runner.invoke(cli_app, ["config", "set", "scan.period", "8h"])
    assert noop.exit_code == 0, noop.output
    assert "no change" in noop.output

    validated = runner.invoke(cli_app, ["config", "validate"])
    assert validated.exit_code == 0, validated.output
    assert "PASS" in validated.output


def test_cli_config_load_failure_exits_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_PORT", "not-a-port")
    runner = CliRunner()

    result = runner.invoke(cli_app, ["config", "show"])

    assert result.exit_code == 5


def test_cli_set_is_idempotent_for_the_same_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()

    first = runner.invoke(cli_app, ["config", "set", "scan.period", "8h"])
    second = runner.invoke(cli_app, ["config", "set", "scan.period", "8h"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    from oft.adapters.sqlite_store import SqliteStore
    from oft.config import load_settings

    store = SqliteStore(load_settings())
    assert len(store.list_config()) == 1
    assert len(store.list_audit_events()) == 1


def _seed_corrupt_persisted_period(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, tmp_path)
    store = SqliteStore(load_settings())
    store.initialize()
    store.set_config(
        ConfigRecord(
            key="scan.period",
            value_json="not-json",
            updated_at="2026-11-06T10:00:00.000Z",
            updated_by="web",
        )
    )


def test_daemon_status_json_keeps_the_error_envelope_for_a_corrupt_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_corrupt_persisted_period(tmp_path, monkeypatch)
    runner = CliRunner()

    result = runner.invoke(cli_app, ["daemon", "status", "--json"])

    assert result.exit_code == 5, result.output
    body = json.loads(result.output)
    assert body["ok"] is False
    assert body["error"]["code"] == "CONFIG_INVALID"
    assert body["data"] is None


def test_scan_status_json_keeps_the_error_envelope_for_a_corrupt_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_corrupt_persisted_period(tmp_path, monkeypatch)
    runner = CliRunner()

    result = runner.invoke(cli_app, ["scan", "status", "--json"])

    assert result.exit_code == 5, result.output
    body = json.loads(result.output)
    assert body["ok"] is False
    assert body["error"]["code"] == "CONFIG_INVALID"


# -- M3/M4/M14 daemon seams ---------------------------------------------------


def test_daemon_and_scan_status_show_the_persisted_period_over_an_invalid_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_persisted_period(monkeypatch, tmp_path, "8h")
    # A stale environment value must not override a valid saved period.
    monkeypatch.setenv("OFT_SCAN_PERIOD", "1h")
    runner = CliRunner()

    daemon = runner.invoke(cli_app, ["daemon", "status", "--json"])
    assert daemon.exit_code == 0, daemon.output
    assert json.loads(daemon.output)["data"]["period"] == "8h"

    scan = runner.invoke(cli_app, ["scan", "status", "--json"])
    assert scan.exit_code == 0, scan.output
    assert json.loads(scan.output)["data"]["period"] == "8h"

    show = runner.invoke(cli_app, ["config", "show", "--json"])
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["data"]["source"] == "user"


def test_an_out_of_range_jitter_is_clamped_for_a_smaller_persisted_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_persisted_period(monkeypatch, tmp_path, "4h")
    # 20000s exceeds the 4h period (14400s); the overlay clamps instead of
    # raising from ScheduleState.
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "20000")
    runner = CliRunner()

    result = runner.invoke(cli_app, ["daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["period"] == "4h"


def test_a_non_integer_jitter_is_config_invalid_even_with_a_persisted_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_persisted_period(monkeypatch, tmp_path, "8h")
    monkeypatch.setenv("OFT_SCAN_JITTER_SECONDS", "soon")
    runner = CliRunner()

    result = runner.invoke(cli_app, ["daemon", "status", "--json"])

    assert result.exit_code == 5, result.output
    assert json.loads(result.output)["error"]["code"] == "CONFIG_INVALID"
