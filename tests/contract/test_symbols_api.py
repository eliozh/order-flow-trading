"""Contract tests for the read-only symbols/watchlist API and CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typer.testing import CliRunner

from oft.api.app import create_app
from oft.bootstrap import build_container
from oft.cli.main import app as cli_app
from oft.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURE = ROOT / "tests" / "fixtures" / "binance" / "exchange_info_sample.json"


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


def _configure_cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", str(settings.port))
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_LOG_LEVEL", "INFO")


def test_symbols_list_validates_and_contains_fixture_rows(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols", headers=_auth(app))

    assert response.status_code == 200
    body = response.json()
    _validate(validator, body)
    assert body["ok"] is True
    data = body["data"]
    assert data["total"] == 3
    assert [item["symbol"] for item in data["items"]] == ["BTCUSDT", "ETHUSDT", "XAUUSDT"]

    btc = data["items"][0]
    assert btc["contract_type"] == "PERPETUAL"
    assert btc["tick_size"] == "0.10"
    assert btc["step_size"] == "0.001"
    assert btc["min_qty"] == "0.001"
    assert btc["min_notional"] == "5.00"
    assert btc["price_precision"] == 2
    assert btc["quantity_precision"] == 3
    assert btc["onboard_date"] == 1567900800000
    assert btc["listed_date"] == "2019-09-08"
    assert btc["underlying_type"] == "COIN"

    xau = data["items"][2]
    assert xau["contract_type"] == "TRADIFI_PERPETUAL"
    assert xau["tradifi"] is True
    assert xau["listed_date"] == "2025-12-11"

    pagination = body["meta"]["pagination"]
    assert pagination == {
        "limit": 50,
        "cursor": None,
        "has_next": False,
        "next_cursor": None,
        "total": 3,
    }


def test_symbols_search_exact_and_substring(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        exact = client.get("/api/v1/symbols", params={"q": "XAUUSDT"}, headers=_auth(app))
        substring = client.get("/api/v1/symbols", params={"q": "usdt"}, headers=_auth(app))

    assert [item["symbol"] for item in exact.json()["data"]["items"]] == ["XAUUSDT"]
    assert [item["symbol"] for item in substring.json()["data"]["items"]] == [
        "BTCUSDT",
        "ETHUSDT",
        "XAUUSDT",
    ]
    _validate(validator, exact.json())
    _validate(validator, substring.json())


def test_symbols_filter_by_group_and_contract_type(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        tradfi = client.get("/api/v1/symbols", params={"group": "tradfi"}, headers=_auth(app))
        crypto = client.get("/api/v1/symbols", params={"group": "crypto"}, headers=_auth(app))
        perpetual = client.get(
            "/api/v1/symbols", params={"contract_type": "PERPETUAL"}, headers=_auth(app)
        )

    assert [item["symbol"] for item in tradfi.json()["data"]["items"]] == ["XAUUSDT"]
    assert [item["symbol"] for item in crypto.json()["data"]["items"]] == ["BTCUSDT", "ETHUSDT"]
    assert [item["symbol"] for item in perpetual.json()["data"]["items"]] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    for response in (tradfi, crypto, perpetual):
        _validate(validator, response.json())


def test_symbols_pagination_uses_the_envelope_meta(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/symbols", params={"limit": 1, "offset": 1}, headers=_auth(app)
        )

    body = response.json()
    _validate(validator, body)
    assert [item["symbol"] for item in body["data"]["items"]] == ["ETHUSDT"]
    assert body["meta"]["pagination"] == {
        "limit": 1,
        "cursor": None,
        "has_next": True,
        "next_cursor": "2",
        "total": 3,
    }


def test_symbol_detail_returns_specs_and_coverage_placeholders(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols/BTCUSDT", headers=_auth(app))

    assert response.status_code == 200
    body = response.json()
    _validate(validator, body)
    contract = body["data"]["contract"]
    assert contract["symbol"] == "BTCUSDT"
    assert contract["min_notional"] == "5.00"
    coverage = body["data"]["coverage"]
    assert coverage["status"] == "UNKNOWN"
    assert coverage["coverage_pct"] is None
    assert coverage["history_start"] == "2019-09-08"
    assert coverage["book_ticker"] == "N/A"
    assert coverage["liquidation_history"] == "SINCE_DEPLOYMENT"
    assert coverage["latest_metric_at"] is None
    assert coverage["notes"]


def test_unknown_symbol_returns_not_found(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols/NOPEUSDT", headers=_auth(app))

    assert response.status_code == 404
    body = response.json()
    _validate(validator, body)
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"


def test_watchlist_is_read_only_and_grouped(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/watchlist", headers=_auth(app))
        mutation = client.post("/api/v1/watchlist", json={"symbol": "BTCUSDT"}, headers=_auth(app))

    assert response.status_code == 200
    body = response.json()
    _validate(validator, body)
    groups = {group["name"]: group for group in body["data"]["groups"]}
    assert groups["CRYPTO"]["count"] == 2
    assert groups["TRADFI"]["count"] == 1
    assert groups["TRADFI"]["symbols"] == ["XAUUSDT"]
    assert body["data"]["total"] == 3

    assert mutation.status_code == 405
    mutation_body = mutation.json()
    _validate(validator, mutation_body)
    assert mutation_body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_cli_search_returns_the_same_specs_as_the_api(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    validator: Draft202012Validator,
) -> None:
    _configure_cli(monkeypatch, settings)
    build_container(settings, initialize=True)
    app = create_app(settings)

    with TestClient(app) as client:
        api_contract = client.get("/api/v1/symbols/BTCUSDT", headers=_auth(app)).json()["data"][
            "contract"
        ]

    result = CliRunner().invoke(cli_app, ["watchlist", "search", "BTCUSDT", "--format", "json"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    _validate(validator, envelope)
    assert envelope["ok"] is True
    item = envelope["data"]["items"][0]
    assert item["symbol"] == "BTCUSDT"
    assert item["tick_size"] == api_contract["tick_size"]
    assert item["step_size"] == api_contract["step_size"]
    assert item["min_notional"] == api_contract["min_notional"]
    assert item["listed_date"] == api_contract["listed_date"]


def test_cli_groups_reports_crypto_and_tradfi_counts(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    validator: Draft202012Validator,
) -> None:
    _configure_cli(monkeypatch, settings)
    build_container(settings, initialize=True)

    result = CliRunner().invoke(cli_app, ["watchlist", "groups", "--format", "json"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    _validate(validator, envelope)
    assert envelope["data"]["crypto"] == 2
    assert envelope["data"]["tradfi"] == 1
    assert envelope["data"]["total"] == 3


def test_cli_list_renders_a_stable_read_only_table(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_cli(monkeypatch, settings)
    build_container(settings, initialize=True)

    result = CliRunner().invoke(cli_app, ["watchlist", "list"])

    assert result.exit_code == 0, result.output
    assert "CRYPTO (2)" in result.stdout
    assert "TRADFI (1)" in result.stdout
    assert "BTCUSDT" in result.stdout
    assert "XAUUSDT" in result.stdout
    assert "TRADIFI_PERPETUAL" in result.stdout


def test_cli_without_a_registry_reports_empty_not_an_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_cli(monkeypatch, settings)

    result = CliRunner().invoke(cli_app, ["watchlist", "list"])

    assert result.exit_code == 0, result.output
    assert "No symbols" in result.stdout


def test_offset_above_the_bound_is_validation_failed(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols", params={"offset": 10_000_001}, headers=_auth(app))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_overflow_from_the_store_maps_to_validation_failed(settings: Settings) -> None:
    app = create_app(settings)

    def boom(**_: object) -> object:
        raise OverflowError("offset too large")

    app.state.container.symbol_registry.list_symbols = boom  # type: ignore[method-assign]

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols", headers=_auth(app))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_watchlist_uses_one_snapshot_read(settings: Settings) -> None:
    app = create_app(settings)
    registry = app.state.container.symbol_registry
    calls: list[dict[str, Any]] = []
    original = registry.watchlist_snapshot

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(**kwargs)

    registry.watchlist_snapshot = spy  # type: ignore[method-assign]

    with TestClient(app) as client:
        response = client.get("/api/v1/watchlist", headers=_auth(app))

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["limit"] == 2000


_STORE_FAILURE_COMMANDS = [
    ["watchlist", "list", "--format", "json"],
    ["watchlist", "search", "BTCUSDT", "--format", "json"],
    ["watchlist", "groups", "--format", "json"],
]


@pytest.mark.parametrize("command", _STORE_FAILURE_COMMANDS)
def test_cli_store_failure_reports_data_unavailable(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    import oft.cli.commands.watchlist as watchlist_module
    from oft.ports.store import StoreUnavailableError

    class _BoomRegistry:
        def list_symbols(self, **_: Any) -> Any:
            raise StoreUnavailableError("raw sqlite /secret/path")

        def group_counts(self) -> Any:
            raise StoreUnavailableError("raw sqlite /secret/path")

    monkeypatch.setattr(watchlist_module, "_open_registry", lambda json_output: _BoomRegistry())

    result = CliRunner().invoke(cli_app, command)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DATA_UNAVAILABLE"
    assert "secret" not in result.output


def test_star_and_unstar_symbol_via_patch(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        starred = client.patch(
            "/api/v1/symbols/BTCUSDT/star", json={"starred": True}, headers=_auth(app)
        )
        filtered = client.get("/api/v1/symbols", params={"starred": "true"}, headers=_auth(app))
        unstarred = client.patch(
            "/api/v1/symbols/BTCUSDT/star", json={"starred": False}, headers=_auth(app)
        )
        empty = client.get("/api/v1/symbols", params={"starred": "true"}, headers=_auth(app))

    assert starred.status_code == 200
    body = starred.json()
    _validate(validator, body)
    assert body["data"] == {
        "symbol": "BTCUSDT",
        "starred": True,
        "starred_at": body["data"]["starred_at"],
    }
    assert body["data"]["starred_at"]
    assert [item["symbol"] for item in filtered.json()["data"]["items"]] == ["BTCUSDT"]
    assert filtered.json()["data"]["total"] == 1
    assert unstarred.status_code == 200
    assert unstarred.json()["data"]["starred"] is False
    assert empty.json()["data"]["items"] == []


def test_star_unknown_symbol_is_not_found(
    settings: Settings, validator: Draft202012Validator
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/symbols/NOPEUSDT/star", json={"starred": True}, headers=_auth(app)
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_star_requires_a_boolean_body(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.patch("/api/v1/symbols/BTCUSDT/star", json={}, headers=_auth(app))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_cli_star_unstar_and_starred_list(
    live_daemon: Settings, validator: Draft202012Validator
) -> None:
    runner = CliRunner()

    starred = runner.invoke(cli_app, ["watchlist", "star", "BTCUSDT", "--json"])
    assert starred.exit_code == 0, starred.output
    assert json.loads(starred.stdout)["data"]["starred"] is True

    listed = runner.invoke(cli_app, ["watchlist", "list", "--starred", "--format", "json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    _validate(validator, payload)
    assert [item["symbol"] for item in payload["data"]["items"]] == ["BTCUSDT"]

    unstarred = runner.invoke(cli_app, ["watchlist", "unstar", "BTCUSDT", "--json"])
    assert unstarred.exit_code == 0, unstarred.output
    empty = runner.invoke(cli_app, ["watchlist", "list", "--starred", "--format", "json"])
    assert json.loads(empty.stdout)["data"]["items"] == []


def test_cli_star_unknown_symbol_exits_nonzero(live_daemon: Settings) -> None:
    result = CliRunner().invoke(cli_app, ["watchlist", "star", "NOPEUSDT", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "NOT_FOUND"


def test_cli_star_without_daemon_reports_daemon_not_running(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_cli(monkeypatch, settings)

    result = CliRunner().invoke(cli_app, ["watchlist", "star", "BTCUSDT", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "DAEMON_NOT_RUNNING"
