"""Integration: a review is idempotent per trade and ``--refresh`` retains history."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from test_review_flow import FIXTURES, _closed_trade, _review_service

from oft.adapters.llm_stub import LLMStub
from oft.api.app import create_app
from oft.clock import FixedClock
from oft.ports.llm import LLMRole

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _provider() -> LLMStub:
    return LLMStub(fixtures_dir=FIXTURES, overrides={LLMRole.REVIEW: "review_valid.json"})


class _Counting:
    provider = "stub"

    def __init__(self) -> None:
        self._inner = _provider()
        self.calls = 0

    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._inner.capabilities()

    def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._inner.complete(request)

    def estimate_cost(self, request, model):  # type: ignore[no-untyped-def]
        return self._inner.estimate_cost(request, model)


def test_a_second_review_reuses_the_linked_decision_with_no_provider_call(
    tmp_path: Path,
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    provider = _Counting()
    service = _review_service(container, provider)

    first = service.review(trade["trade_id"], idempotency_key="key-a")
    second = service.review(trade["trade_id"], idempotency_key="key-b")

    assert second.decision_id == first.decision_id
    assert second.idempotent_replay is True
    assert provider.calls == 1
    assert len(container.store.list_decisions(role="REVIEW")) == 1


def test_refresh_creates_a_new_decision_and_keeps_the_old_one_readable(
    tmp_path: Path,
) -> None:
    _settings, container, _position, trade = _closed_trade(tmp_path)
    provider = _Counting()
    service = _review_service(container, provider)
    trade_id = trade["trade_id"]
    first = service.review(trade_id, idempotency_key="refresh-1")
    metrics_before = json.loads(container.store.get_trade_log(trade_id).payload_json)  # type: ignore[union-attr]

    second = service.review(trade_id, refresh=True, idempotency_key="refresh-2")

    assert second.decision_id != first.decision_id
    assert second.idempotent_replay is False
    assert provider.calls == 2
    assert len(container.store.list_decisions(role="REVIEW")) == 2
    assert container.store.get_decision(first.decision_id) is not None
    linked = container.store.get_trade_log(trade_id)
    assert linked is not None and linked.review_decision_id == second.decision_id
    after = json.loads(linked.payload_json)
    for field in ("gross_pnl_usd", "fees_usd", "funding_usd", "net_pnl_usd", "r_multiple"):
        assert after[field] == metrics_before[field]

    replay = service.review(trade_id, refresh=True, idempotency_key="refresh-2")

    assert replay.decision_id == second.decision_id
    assert replay.idempotent_replay is True
    assert provider.calls == 2
    assert len(container.store.list_decisions(role="REVIEW")) == 2


def test_the_api_replays_a_duplicate_review_request(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    key = {"Idempotency-Key": "api-review-dup"}

    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/journal/{trade['trade_id']}/review", headers={**headers, **key}
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/v1/journal/{trade['trade_id']}/review", headers={**headers, **key}
        )
        assert second.status_code == 200, second.text

    assert second.json()["meta"]["idempotent_replay"] is True
    assert second.json()["data"]["decision_id"] == first.json()["data"]["decision_id"]
    assert len(container.store.list_decisions(role="REVIEW")) == 1


def test_the_api_requires_an_idempotency_key(tmp_path: Path) -> None:
    settings, _container, _position, trade = _closed_trade(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.post(f"/api/v1/journal/{trade['trade_id']}/review", headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_provider_case_and_whitespace_replay_without_a_conflict(tmp_path: Path) -> None:
    settings, container, _position, trade = _closed_trade(tmp_path)
    app = create_app(settings, clock=FixedClock(NOW))
    headers = {"Authorization": f"Bearer {app.state.api_token}"}
    trade_id = trade["trade_id"]
    # The CLI derives this key from the stripped/lowered provider, so every
    # case/whitespace variant must hash the same normalized request.
    key = {"Idempotency-Key": f"review:{trade_id}:stub"}

    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/journal/{trade_id}/review?provider=Stub",
            headers={**headers, **key},
        )
        padded = client.post(
            f"/api/v1/journal/{trade_id}/review?provider=%20stub%20",
            headers={**headers, **key},
        )
        plain = client.post(
            f"/api/v1/journal/{trade_id}/review?provider=stub",
            headers={**headers, **key},
        )

    assert first.status_code == 200, first.text
    assert padded.status_code == 200, padded.text
    assert plain.status_code == 200, plain.text
    first_id = first.json()["data"]["decision_id"]
    assert padded.json()["data"]["decision_id"] == first_id
    assert plain.json()["data"]["decision_id"] == first_id
    assert padded.json()["meta"]["idempotent_replay"] is True
    assert plain.json()["meta"]["idempotent_replay"] is True
    assert len(container.store.list_decisions(role="REVIEW")) == 1
