"""Watchlist POST/DELETE star endpoints, idempotency, audit, and GET extras."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.cli.main import app as cli_app
from oft.config import Settings


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


def _audit_bodies(store: Any) -> list[dict[str, Any]]:
    return [json.loads(record.payload_json) for record in store.list_audit_events()]


def test_post_star_twice_is_one_star_and_one_audit(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        first = client.post("/api/v1/watchlist/BTCUSDT", headers=_auth(app))
        second = client.post("/api/v1/watchlist/BTCUSDT", headers=_auth(app))

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    payload = first.json()["data"]
    assert payload["symbol"] == "BTCUSDT"
    assert payload["starred"] is True
    assert payload["starred_at"]
    assert second.json()["data"] == payload

    audits = app.state.container.store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "WATCHLIST_STAR_TOGGLED"
    body = _audit_bodies(app.state.container.store)[0]
    assert body["payload"]["before"] == {"starred": False, "starred_at": None}
    assert body["payload"]["after"] == {
        "starred": True,
        "starred_at": payload["starred_at"],
    }


def test_delete_unstar_twice_is_one_change(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        client.post("/api/v1/watchlist/BTCUSDT", headers=_auth(app))
        first = client.delete("/api/v1/watchlist/BTCUSDT", headers=_auth(app))
        second = client.delete("/api/v1/watchlist/BTCUSDT", headers=_auth(app))

    assert first.status_code == 200
    assert first.json()["data"] == {
        "symbol": "BTCUSDT",
        "starred": False,
        "starred_at": None,
    }
    assert second.json()["data"] == first.json()["data"]
    assert len(app.state.container.store.list_audit_events()) == 2


@pytest.mark.parametrize("method", ["post", "delete"])
def test_unknown_symbol_returns_not_found(settings: Settings, method: str) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = getattr(client, method)("/api/v1/watchlist/NOPEUSDT", headers=_auth(app))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert app.state.container.store.list_audit_events() == ()


def test_get_watchlist_adds_rows_counts_and_star_mutation(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        client.post("/api/v1/watchlist/BTCUSDT", headers=_auth(app))
        client.post("/api/v1/watchlist/ETHUSDT", headers=_auth(app))
        response = client.get("/api/v1/watchlist", headers=_auth(app))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["star_mutation"] is True
    assert data["starred_count"] == 2
    assert data["next_scan_count"] == len(app.state.container.symbol_registry.list_starred()) == 2

    # The existing grouped view is unchanged and still lists every contract.
    groups = {group["name"]: group for group in data["groups"]}
    assert groups["CRYPTO"]["count"] == 2
    assert groups["TRADFI"]["count"] == 1
    assert data["total"] == 3

    rows = {row["symbol"]: row for row in data["rows"]}
    assert set(rows) == {"BTCUSDT", "ETHUSDT", "XAUUSDT"}
    assert rows["BTCUSDT"]["starred"] is True
    assert rows["BTCUSDT"]["starred_at"]
    assert rows["XAUUSDT"]["starred"] is False
    assert rows["XAUUSDT"]["starred_at"] is None
    # Every row still carries the full contract contract, not just the symbol.
    assert rows["BTCUSDT"]["tick_size"] == "0.10"
    assert rows["BTCUSDT"]["listed_date"] == "2019-09-08"


def test_patch_star_stays_compatible_and_updates_the_next_scan_count(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        patched = client.patch(
            "/api/v1/symbols/BTCUSDT/star", json={"starred": True}, headers=_auth(app)
        )
        watchlist = client.get("/api/v1/watchlist", headers=_auth(app))

    assert patched.status_code == 200
    data = patched.json()["data"]
    assert data["symbol"] == "BTCUSDT"
    assert data["starred"] is True
    assert data["starred_at"]
    assert watchlist.json()["data"]["next_scan_count"] == 1
    audits = app.state.container.store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "WATCHLIST_STAR_TOGGLED"


def test_patch_star_attributes_the_cli_surface_from_an_allowlisted_header(
    settings: Settings,
) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        cli = client.patch(
            "/api/v1/symbols/BTCUSDT/star",
            json={"starred": True},
            headers={**_auth(app), "X-OFT-Surface": "cli"},
        )
        web = client.patch(
            "/api/v1/symbols/ETHUSDT/star",
            json={"starred": True},
            headers={**_auth(app), "X-OFT-Surface": "evil"},
        )

    assert cli.status_code == 200
    assert web.status_code == 200
    bodies = _audit_bodies(app.state.container.store)
    actors = {body["subject"]["id"]: body["actor"] for body in bodies}
    assert actors["BTCUSDT"] == {"kind": "USER", "id": "cli"}
    # Any non-allowlisted surface value cannot invent an audit actor.
    assert actors["ETHUSDT"] == {"kind": "USER", "id": "web"}


def test_watchlist_post_and_delete_stay_attributed_to_web(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        client.post(
            "/api/v1/watchlist/BTCUSDT",
            headers={**_auth(app), "X-OFT-Surface": "cli"},
        )
        client.delete("/api/v1/watchlist/BTCUSDT", headers=_auth(app))

    actors = [body["actor"] for body in _audit_bodies(app.state.container.store)]
    assert actors == [
        {"kind": "USER", "id": "web"},
        {"kind": "USER", "id": "web"},
    ]


def test_cli_add_remove_aliases_return_the_star_envelope(live_daemon: Settings) -> None:
    runner = CliRunner()

    added = runner.invoke(cli_app, ["watchlist", "add", "BTCUSDT", "--json"])
    assert added.exit_code == 0, added.output
    added_payload = json.loads(added.stdout)
    assert added_payload["ok"] is True
    assert added_payload["data"]["symbol"] == "BTCUSDT"
    assert added_payload["data"]["starred"] is True
    assert added_payload["data"]["starred_at"]

    removed = runner.invoke(cli_app, ["watchlist", "remove", "BTCUSDT", "--json"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.stdout)["data"] == {
        "symbol": "BTCUSDT",
        "starred": False,
        "starred_at": None,
    }

    # The CLI sends X-OFT-Surface, so both mutations are attributed to it.
    bodies = _audit_bodies(SqliteStore(live_daemon))
    assert len(bodies) == 2
    assert [body["actor"] for body in bodies] == [
        {"kind": "USER", "id": "cli"},
        {"kind": "USER", "id": "cli"},
    ]
