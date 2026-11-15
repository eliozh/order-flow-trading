"""``/api/v1/settings/risk`` integration tests (slice 34)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.cli.main import app as cli_app
from oft.clock import FixedClock
from oft.config import Settings, load_settings
from oft.domain.risk import DEFAULT_CAPS, RISK_CAPS_STATE_KEY
from oft.ports.store import AuditEventDraft

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-06T10:00:00.000Z"
RISK_PATH = "/api/v1/settings/risk"
SECRET_MARKERS = ("api_key", "secret", "password", "token", "openai_api_key")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data", **overrides)  # type: ignore[arg-type]


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _put(
    client: TestClient,
    auth: dict[str, str],
    caps: dict[str, Any],
    *,
    key: str = "risk-key-1",
    confirm: bool = False,
    reason: str = "",
) -> Any:
    return client.put(
        RISK_PATH,
        json={"caps": caps, "confirm": confirm, "reason": reason},
        headers={**auth, "Idempotency-Key": key},
    )


@pytest.fixture(autouse=True)
def _clear_risk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OFT_RISK_PER_TRADE_PCT",
        "OFT_RISK_MAX_CONCURRENT_POSITIONS",
        "OFT_RISK_DAILY_LOSS_PCT",
        "OFT_RISK_MAX_LEVERAGE",
        "OFT_RISK_MAX_NOTIONAL_PCT",
        "OFT_RISK_MAX_SLIPPAGE_BPS",
        "OFT_RISK_MAX_SPREAD_BPS",
        "OFT_RISK_STALE_MARK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_get_risk_settings_returns_effective_caps_defaults_and_utilization(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = client.get(RISK_PATH, headers=_auth(app))

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True
    data = envelope["data"]
    assert set(data) == {"caps", "source", "defaults", "utilization", "updated_at", "updated_by"}
    assert data["source"] == "default"
    assert data["caps"] == DEFAULT_CAPS.to_state()
    assert data["defaults"] == DEFAULT_CAPS.to_state()
    assert data["updated_at"] is None
    assert data["updated_by"] is None
    utilization = data["utilization"]
    assert set(utilization) == {"source", "quality", "stale_inputs", "metrics"}
    assert utilization["source"] == "none"
    assert utilization["quality"] == "UNAVAILABLE"
    assert set(utilization["metrics"]) == set(DEFAULT_CAPS.to_state())
    for metric in utilization["metrics"].values():
        assert set(metric) == {"used", "cap", "unit", "source", "quality", "note"}
    assert not any(marker in json.dumps(data).lower() for marker in SECRET_MARKERS)
    # A GET never writes.
    assert app.state.container.store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert app.state.container.store.list_audit_events() == ()


def test_get_risk_settings_reads_the_persisted_row_and_last_change(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    store.set_system_state(
        RISK_CAPS_STATE_KEY, json.dumps({"per_trade_risk_pct": 0.5}), updated_at=NOW_TEXT
    )
    store.append_audit_event(
        AuditEventDraft(
            event_type="RISK_CAPS_UPDATED",
            actor={"kind": "USER", "id": "web"},
            subject={"kind": "SYSTEM", "id": RISK_CAPS_STATE_KEY},
            payload={"after": {"per_trade_risk_pct": 0.5}},
            created_at=NOW_TEXT,
        )
    )

    with TestClient(app) as client:
        data = client.get(RISK_PATH, headers=_auth(app)).json()["data"]

    assert data["source"] == "user"
    assert data["caps"]["per_trade_risk_pct"] == 0.5
    assert data["caps"]["max_leverage"] == DEFAULT_CAPS.max_leverage
    assert data["defaults"]["per_trade_risk_pct"] == DEFAULT_CAPS.per_trade_risk_pct
    assert data["updated_at"] == NOW_TEXT
    assert data["updated_by"] == "web"


def test_tightening_put_writes_one_row_and_exactly_one_audit(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        response = _put(client, auth, {"per_trade_risk_pct": 0.5}, key="tighten-1")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["caps"]["per_trade_risk_pct"] == 0.5
    assert data["source"] == "user"
    assert data["updated_at"] == NOW_TEXT
    assert data["updated_by"] == "web"

    store = app.state.container.store
    audits = store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "RISK_CAPS_UPDATED"
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["before"]["per_trade_risk_pct"] == DEFAULT_CAPS.per_trade_risk_pct
    assert payload["after"]["per_trade_risk_pct"] == 0.5
    assert payload["loosened"] == []
    row = store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)["per_trade_risk_pct"] == 0.5


def test_loosening_without_confirm_is_refused_with_per_cap_details_and_writes_nothing(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"max_leverage": 6.0}, key="loosen-1")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["details"]
    assert all(detail["code"] == "confirmation_required" for detail in error["details"])
    assert [detail["field"] for detail in error["details"]] == ["max_leverage"]

    store = app.state.container.store
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_confirmed_loosening_writes_and_audits_before_and_after(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(
            client,
            _auth(app),
            {"max_leverage": 6.0},
            key="loosen-confirm",
            confirm=True,
            reason="operator review",
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["caps"]["max_leverage"] == 6.0
    audits = app.state.container.store.list_audit_events()
    assert len(audits) == 1
    payload = json.loads(audits[0].payload_json)["payload"]
    assert payload["before"]["max_leverage"] == 5.0
    assert payload["after"]["max_leverage"] == 6.0
    assert payload["loosened"] == ["max_leverage"]
    assert payload["reason"] == "operator review"


@pytest.mark.parametrize(
    ("caps", "field"),
    [
        ({"per_trade_risk_pct": -1}, "per_trade_risk_pct"),
        ({"per_trade_risk_pct": 101}, "per_trade_risk_pct"),
        ({"daily_loss_pct": 0.5}, "daily_loss_pct"),
        ({"max_leverage": 126}, "max_leverage"),
        ({"max_concurrent_positions": 0}, "max_concurrent_positions"),
        ({"stale_mark_seconds": 0}, "stale_mark_seconds"),
        ({"per_trade_risk_pct": True}, "per_trade_risk_pct"),
        ({}, None),
    ],
)
def test_invalid_values_return_field_details_and_write_nothing(
    tmp_path: Path, caps: dict[str, Any], field: str | None
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), caps, key="invalid-1", confirm=True)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    if field is not None:
        assert field in [detail.get("field") for detail in error["details"]]
    store = app.state.container.store
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_unknown_nested_cap_is_rejected_with_no_write(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"bogus": 1}, key="unknown-cap")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert [detail.get("field") for detail in error["details"]] == ["bogus"]
    assert error["details"][0]["code"] == "unknown_field"
    store = app.state.container.store
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None
    assert store.list_audit_events() == ()


def test_put_over_a_corrupt_persisted_row_is_config_invalid_with_no_write(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    store.set_system_state(RISK_CAPS_STATE_KEY, "not-json", updated_at=NOW_TEXT)

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"max_leverage": 3.0}, key="corrupt-put")

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert store.get_system_state(RISK_CAPS_STATE_KEY) == "not-json"
    assert store.list_audit_events() == ()


def test_put_over_an_invalid_persisted_document_is_config_invalid_with_no_write(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    store.set_system_state(
        RISK_CAPS_STATE_KEY,
        json.dumps({"per_trade_risk_pct": -1}),
        updated_at=NOW_TEXT,
    )

    with TestClient(app) as client:
        response = _put(client, _auth(app), {"max_leverage": 3.0}, key="invalid-put")

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert (
        json.loads(store.get_system_state(RISK_CAPS_STATE_KEY) or "{}")["per_trade_risk_pct"] == -1
    )
    assert store.list_audit_events() == ()


def test_legacy_cross_field_row_is_readable_and_repairable_via_api(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    store.set_system_state(
        RISK_CAPS_STATE_KEY,
        json.dumps({"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5}),
        updated_at=NOW_TEXT,
    )
    auth = _auth(app)

    with TestClient(app) as client:
        sections = client.get("/api/v1/settings", headers=auth)
        risk = client.get(RISK_PATH, headers=auth)
        repaired = _put(client, auth, {"daily_loss_pct": 3.0}, key="repair-1", confirm=True)

    assert sections.status_code == 200, sections.text
    assert [section["section"] for section in sections.json()["data"]["sections"]] == [
        "scan",
        "risk",
    ]
    assert risk.status_code == 200, risk.text
    assert risk.json()["data"]["caps"]["daily_loss_pct"] == 0.5
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["caps"]["daily_loss_pct"] == 3.0
    audits = store.list_audit_events()
    assert len(audits) == 1
    row = json.loads(store.get_system_state(RISK_CAPS_STATE_KEY) or "{}")
    assert row["daily_loss_pct"] == 3.0
    assert row["per_trade_risk_pct"] == 0.8


def test_put_of_a_still_violating_proposed_doc_is_rejected_and_writes_nothing(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    store = app.state.container.store
    legacy = {"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5}
    store.set_system_state(RISK_CAPS_STATE_KEY, json.dumps(legacy), updated_at=NOW_TEXT)

    with TestClient(app) as client:
        response = _put(
            client, _auth(app), {"per_trade_risk_pct": 2.0}, key="still-bad", confirm=True
        )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert any(detail.get("code") == "daily_loss_below_per_trade" for detail in error["details"])
    assert store.list_audit_events() == ()
    assert json.loads(store.get_system_state(RISK_CAPS_STATE_KEY) or "{}") == legacy


def test_identical_idempotent_replay_writes_nothing_and_a_different_payload_conflicts(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"per_trade_risk_pct": 0.5}, key="stable")
        replay = _put(client, auth, {"per_trade_risk_pct": 0.5}, key="stable")
        conflict = _put(client, auth, {"per_trade_risk_pct": 0.4}, key="stable")

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert replay.json()["data"] == first.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(app.state.container.store.list_audit_events()) == 1
    row = app.state.container.store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)["per_trade_risk_pct"] == 0.5


def test_concurrent_partial_saves_are_serialized_with_no_lost_update(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    def tighten_per_trade() -> Any:
        with TestClient(app) as client:
            return _put(client, auth, {"per_trade_risk_pct": 0.5}, key="concurrent-a")

    def tighten_leverage() -> Any:
        with TestClient(app) as client:
            return _put(client, auth, {"max_leverage": 3.0}, key="concurrent-b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(tighten_per_trade), pool.submit(tighten_leverage)]
        responses = [future.result() for future in results]

    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    store = app.state.container.store
    row = store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    caps = json.loads(row)
    assert caps["per_trade_risk_pct"] == 0.5
    assert caps["max_leverage"] == 3.0
    audits = store.list_audit_events()
    assert len(audits) == 2
    assert {audit.event_type for audit in audits} == {"RISK_CAPS_UPDATED"}


def test_same_effective_value_with_a_fresh_key_is_a_noop(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    auth = _auth(app)

    with TestClient(app) as client:
        first = _put(client, auth, {"per_trade_risk_pct": 0.5}, key="first")
        assert first.status_code == 200, first.text
        before = app.state.container.store.list_audit_events()
        second = _put(client, auth, {"per_trade_risk_pct": 0.5}, key="second")

    assert second.status_code == 200, second.text
    assert second.json()["meta"]["idempotent_replay"] is False
    assert app.state.container.store.list_audit_events() == before


# -- CLI parity --------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    for name in (
        "OFT_RISK_PER_TRADE_PCT",
        "OFT_RISK_MAX_CONCURRENT_POSITIONS",
        "OFT_RISK_DAILY_LOSS_PCT",
        "OFT_RISK_MAX_LEVERAGE",
        "OFT_RISK_MAX_NOTIONAL_PCT",
        "OFT_RISK_MAX_SLIPPAGE_BPS",
        "OFT_RISK_MAX_SPREAD_BPS",
        "OFT_RISK_STALE_MARK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cli_and_api_share_the_same_effective_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    app = create_app(_settings(tmp_path), clock=FixedClock(NOW))
    runner = CliRunner()

    with TestClient(app) as client:
        saved = _put(client, _auth(app), {"per_trade_risk_pct": 0.5}, key="parity-1")
        assert saved.status_code == 200, saved.text
        api_data = client.get(RISK_PATH, headers=_auth(app)).json()["data"]

    shown = runner.invoke(cli_app, ["risk", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    cli_data = json.loads(shown.output)["data"]
    assert cli_data["caps"] == api_data["caps"]
    assert cli_data["source"] == "persisted"
    assert cli_data["defaults"] == api_data["defaults"]

    tightened = runner.invoke(cli_app, ["risk", "set", "--max-notional-pct", "10", "--json"])
    assert tightened.exit_code == 0, tightened.output
    with TestClient(app) as client:
        after_api = client.get(RISK_PATH, headers=_auth(app)).json()["data"]
    assert after_api["caps"]["max_notional_pct"] == 10.0
    assert after_api["caps"]["per_trade_risk_pct"] == 0.5


def test_cli_validate_reports_a_persisted_daily_loss_below_per_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    store = SqliteStore(load_settings())
    store.initialize()
    # ``validate_caps`` owns the cross-field rule, so ``risk validate`` reports
    # the legacy document as FAIL while the row stays readable/repairable.
    store.set_system_state(
        RISK_CAPS_STATE_KEY,
        json.dumps({"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5}),
        updated_at=NOW_TEXT,
    )

    result = CliRunner().invoke(cli_app, ["risk", "validate", "--json"])

    assert result.exit_code == 1, result.output
    body = json.loads(result.output)
    assert body["ok"] is True
    assert body["data"]["status"] == "FAIL"
    assert any("daily_loss_pct" in error for error in body["data"]["errors"])


def test_cli_show_and_set_repair_a_legacy_cross_field_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)
    store = SqliteStore(load_settings())
    store.initialize()
    store.set_system_state(
        RISK_CAPS_STATE_KEY,
        json.dumps({"per_trade_risk_pct": 0.8, "daily_loss_pct": 0.5}),
        updated_at=NOW_TEXT,
    )
    runner = CliRunner()

    shown = runner.invoke(cli_app, ["risk", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["data"]["caps"]["daily_loss_pct"] == 0.5

    repaired = runner.invoke(
        cli_app, ["risk", "set", "--daily-loss-pct", "3", "--confirm", "--json"]
    )
    assert repaired.exit_code == 0, repaired.output

    verified = SqliteStore(load_settings())
    audits = verified.list_audit_events()
    assert len(audits) == 1
    row = json.loads(verified.get_system_state(RISK_CAPS_STATE_KEY) or "{}")
    assert row["daily_loss_pct"] == 3.0
    assert row["per_trade_risk_pct"] == 0.8


def test_cli_set_still_requires_confirm_for_loosening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, tmp_path)

    refused = CliRunner().invoke(cli_app, ["risk", "set", "--per-trade-pct", "1.0", "--json"])
    assert refused.exit_code != 0
    store = SqliteStore(load_settings())
    assert store.get_system_state(RISK_CAPS_STATE_KEY) is None

    accepted = CliRunner().invoke(
        cli_app,
        ["risk", "set", "--per-trade-pct", "1.0", "--confirm", "--reason", "review"],
    )
    assert accepted.exit_code == 0, accepted.output
    store = SqliteStore(load_settings())
    row = store.get_system_state(RISK_CAPS_STATE_KEY)
    assert row is not None
    assert json.loads(row)["per_trade_risk_pct"] == 1.0
