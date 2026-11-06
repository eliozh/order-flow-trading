"""Browser sessions: SPA entry sets an HttpOnly cookie that authenticates API calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.api.auth import SessionStore
from oft.config import Settings


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    return dist


def _app(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("oft.api.app._web_dist", lambda: _fake_dist(tmp_path))
    return create_app(settings)


def _auth(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.api_token}"}


@pytest.mark.parametrize("entry", ["/", "/system", "/watchlist", "/watchlist/BTCUSDT"])
def test_spa_entry_issues_an_httponly_session_cookie(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    app = _app(settings, tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get(entry)

    assert response.status_code == 200
    assert 'id="root"' in response.text
    set_cookie = response.headers["set-cookie"]
    lowered = set_cookie.lower()
    assert "oft_session=" in lowered
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "secure" not in lowered  # loopback HTTP

    session_id = response.cookies.get("oft_session")
    assert session_id
    # The opaque session id never appears in the document.
    assert session_id not in response.text
    assert app.state.api_token not in response.text


def test_session_cookie_authenticates_symbol_api(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)

    with TestClient(app) as client:
        entry = client.get("/watchlist")
        assert entry.status_code == 200
        response = client.get("/api/v1/symbols")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "BTCUSDT" in [item["symbol"] for item in body["data"]["items"]]


def test_no_cookie_and_no_bearer_is_auth_required(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_session_cookie_does_not_bypass_foreign_origin(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)

    with TestClient(app) as client:
        assert client.get("/watchlist").status_code == 200
        response = client.post(
            "/api/v1/scans",
            json={},
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"


def test_session_is_invalid_after_daemon_restart(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _app(settings, tmp_path, monkeypatch)
    with TestClient(first) as client:
        assert client.get("/watchlist").status_code == 200
        session_id = client.cookies.get("oft_session")
    assert session_id

    # A fresh app models a daemon restart: the in-memory session map is gone.
    second = _app(settings, tmp_path, monkeypatch)
    with TestClient(second) as client:
        response = client.get("/api/v1/symbols", headers={"Cookie": f"oft_session={session_id}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_expired_session_is_rejected() -> None:
    sessions = SessionStore(ttl_seconds=-1)
    session_id = sessions.create()

    assert sessions.is_valid(session_id) is False
    assert sessions.is_valid("not-a-session") is False
    assert sessions.is_valid(None) is False


def test_bearer_auth_still_works_without_a_session(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols", headers=_auth(app))

    assert response.status_code == 200
