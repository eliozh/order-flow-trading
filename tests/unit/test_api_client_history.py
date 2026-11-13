"""The history client encodes a position id instead of interpolating it raw."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from oft.cli.api_client import ApiClient


def test_position_history_quotes_the_position_id(monkeypatch: Any) -> None:
    client = ApiClient("http://127.0.0.1:1", None)
    captured: dict[str, str] = {}

    def fake_request(method: str, path: str) -> dict[str, Any]:
        captured["method"] = method
        captured["path"] = path
        return {"data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    position_id = "POS/../weird id"

    client.position_history(position_id)

    assert captured["method"] == "GET"
    assert captured["path"] == (f"/api/v1/positions/{quote(position_id, safe='')}/history")
    assert "/" not in captured["path"].split("/api/v1/positions/", 1)[1].rsplit("/history", 1)[0]
