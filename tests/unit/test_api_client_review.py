"""Unit: the real ApiClient review methods build the documented request."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from oft.cli.api_client import ApiClient


def _capture(monkeypatch: Any) -> dict[str, Any]:
    client = ApiClient("http://127.0.0.1:1", None)
    captured: dict[str, Any] = {}

    def fake_request(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        captured.update(method=method, path=path, payload=payload, headers=extra_headers)
        return {"data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    return client, captured


def test_journal_review_sends_refresh_provider_and_idempotency_key(
    monkeypatch: Any,
) -> None:
    client, captured = _capture(monkeypatch)
    trade_id = "TRD/../weird id"

    client.journal_review(trade_id, refresh=True, provider="stub", idempotency_key="key-1")

    quoted = quote(trade_id, safe="")
    assert quoted not in ("TRD/../weird id",)
    assert captured["method"] == "POST"
    assert captured["path"] == (f"/api/v1/journal/{quoted}/review?refresh=true&provider=stub")
    assert captured["payload"] == {}
    assert captured["headers"] == {"Idempotency-Key": "key-1"}


def test_journal_review_without_options_has_no_query_or_provider(
    monkeypatch: Any,
) -> None:
    client, captured = _capture(monkeypatch)

    client.journal_review("TRD-1", idempotency_key="key-2")

    assert captured["path"] == "/api/v1/journal/TRD-1/review"
    assert captured["headers"] == {"Idempotency-Key": "key-2"}


def test_get_journal_review_quotes_the_trade_id(monkeypatch: Any) -> None:
    client, captured = _capture(monkeypatch)
    trade_id = "TRD/../weird id"

    client.get_journal_review(trade_id)

    assert captured["method"] == "GET"
    assert captured["path"] == (f"/api/v1/journal/{quote(trade_id, safe='')}/review")
    assert captured["payload"] is None
