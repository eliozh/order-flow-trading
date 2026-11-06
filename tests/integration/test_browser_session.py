"""Browser sessions: a one-time bootstrap mints an HttpOnly session cookie."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.api.auth import SessionStore, browser_bootstrap_path
from oft.config import Settings


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    return dist


def _app(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("oft.api.app._web_dist", lambda: _fake_dist(tmp_path))
    return create_app(settings)


def _mint_session(client: TestClient, app: Any) -> str:
    secret = app.state.browser_bootstrap
    response = client.get("/", params={"bootstrap": secret}, follow_redirects=False)
    assert response.status_code == 302
    session_id = response.cookies.get("oft_session")
    assert session_id
    return session_id


def test_bootstrap_file_is_private(settings: Settings) -> None:
    create_app(settings)
    path = browser_bootstrap_path(settings)

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_text(encoding="utf-8")) >= 40


def test_bootstrap_mints_an_httponly_session_and_redirects(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        response = client.get("/", params={"bootstrap": secret}, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        set_cookie = response.headers["set-cookie"].lower()
        assert "oft_session=" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=strict" in set_cookie
        assert "path=/" in set_cookie
        assert "secure" not in set_cookie  # loopback HTTP

        session_id = response.cookies.get("oft_session")
        assert session_id
        assert session_id not in response.text
        assert secret not in response.text

        # The 302 target now serves the SPA because the cookie is valid.
        spa = client.get("/")
        assert spa.status_code == 200
        assert 'id="root"' in spa.text


def test_bootstrap_is_single_use_and_rotates_the_file(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    first_secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        assert (
            client.get("/", params={"bootstrap": first_secret}, follow_redirects=False).status_code
            == 302
        )
        rotated = app.state.browser_bootstrap
        assert rotated != first_secret
        assert browser_bootstrap_path(settings).read_text(encoding="utf-8").strip() == rotated

        # The consumed secret is rejected once the session cookie is gone.
        client.cookies.clear()
        stale = client.get("/", params={"bootstrap": first_secret}, follow_redirects=False)
        assert stale.status_code == 401
        assert first_secret not in stale.text


def test_spa_without_a_session_returns_the_oft_open_prompt(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        response = client.get("/watchlist")

    assert response.status_code == 401
    assert "oft open" in response.text
    assert secret not in response.text
    assert app.state.api_token not in response.text


def test_session_cookie_authenticates_symbol_api(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)

    with TestClient(app) as client:
        _mint_session(client, app)
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
        _mint_session(client, app)
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
        session_id = _mint_session(client, first)

    # A fresh app models a daemon restart: the in-memory session map is gone.
    second = _app(settings, tmp_path, monkeypatch)
    with TestClient(second) as client:
        response = client.get("/api/v1/symbols", headers={"Cookie": f"oft_session={session_id}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_bearer_auth_still_works_without_a_session(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/symbols", headers={"Authorization": f"Bearer {app.state.api_token}"}
        )

    assert response.status_code == 200


def test_session_store_is_bounded_and_purges_expired() -> None:
    sessions = SessionStore(ttl_seconds=60, max_sessions=2)
    first = sessions.create()
    second = sessions.create()
    third = sessions.create()

    assert sessions.is_valid(first) is False  # LRU eviction
    assert sessions.is_valid(second) is True
    assert sessions.is_valid(third) is True
    assert len(sessions) == 2

    expired = SessionStore(ttl_seconds=-1)
    stale = expired.create()
    assert expired.is_valid(stale) is False
    assert len(expired) == 0


def test_logs_never_contain_the_bootstrap_secret(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import logging

    # The in-process httpx TestClient logs request URLs at INFO; the daemon's
    # uvicorn access log is disabled and its structured log must stay clean.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = _app(settings, tmp_path, monkeypatch)
    secret = app.state.browser_bootstrap
    startup = capsys.readouterr()
    startup_text = startup.out + startup.err
    assert secret not in startup_text
    assert "oft open" in startup_text

    with TestClient(app) as client:
        assert (
            client.get("/", params={"bootstrap": secret}, follow_redirects=False).status_code == 302
        )
    rotated = capsys.readouterr()
    rotated_text = rotated.out + rotated.err
    assert secret not in rotated_text
    assert app.state.browser_bootstrap not in rotated_text
    assert "oft open" in rotated_text


def test_non_ascii_bootstrap_is_rejected_without_a_server_error(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/", params={"bootstrap": "é"}, follow_redirects=False)

    assert response.status_code == 401
    assert "oft open" in response.text
