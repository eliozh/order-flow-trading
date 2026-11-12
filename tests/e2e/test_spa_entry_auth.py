"""E2E: slice-20 SPA entry routes require a browser session before serving HTML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.api.app import create_app
from oft.config import Settings


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    return dist


def _app(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("oft.api.app._web_dist", lambda: _fake_dist(tmp_path))
    return create_app(settings)


@pytest.mark.parametrize("path", ["/triggers", "/triggers/TRG-1/", "/paper"])
def test_trigger_and_paper_deep_links_require_auth(
    path: str,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 401
    assert "oft open" in response.text
    assert secret not in response.text
    assert app.state.api_token not in response.text


def test_authorized_trigger_deep_link_serves_the_spa_shell(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    secret = app.state.browser_bootstrap

    with TestClient(app) as client:
        minted = client.get("/", params={"bootstrap": secret}, follow_redirects=False)
        assert minted.status_code == 302
        response = client.get("/triggers")

    assert response.status_code == 200
    assert 'id="root"' in response.text
