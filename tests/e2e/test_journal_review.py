"""E2E: the advisory LLM trade review under /journal/:tradeId.

A real loopback-free in-process daemon runs over a temporary data dir with the
committed archive fixtures and the deterministic ``stub`` provider. A closed
trade is seeded through the real paper path; the review read is MISSING, the
CSRF + Idempotency-Key POST persists one FOUND review linked from the trade, and
a replay returns the stored result. A forced provider failure leaves the trade
metrics and comparison readable. Static source checks prove the review band
offers no rule-change action and renders no raw HTML, prompt, or secret.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import oft.api.routes.journal as journal_route
from oft.api.app import create_app
from oft.api.auth import CSRF_COOKIE, CSRF_HEADER, allowed_origins
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.position_service import PositionService
from oft.application.review_service import ReviewService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMAdapterError
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CONTRACTS = ROOT / "docs" / "contracts"
WEB_SRC = ROOT / "web" / "src"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


@dataclass
class _Harness:
    app: Any
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    container: Any


class _FailingProvider:
    """Delegates capability/cost reads to the stub but fails every completion."""

    provider = "stub"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def complete(self, request: Any) -> Any:
        raise LLMAdapterError("review provider down")

    def estimate_cost(self, request: Any, model: str) -> Any:
        return self._inner.estimate_cost(request, model)


def _registry() -> Registry:
    resources = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append(
            (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def _validate_envelope(envelope: dict[str, Any]) -> None:
    document = json.loads((CONTRACTS / "api-envelope.schema.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(document, registry=_registry()).iter_errors(envelope),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def _harness(tmp_path: Path) -> _Harness:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    app = create_app(settings, clock=FixedClock(NOW))
    client = TestClient(app)
    container = app.state.container
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    return _Harness(
        app=app,
        client=client,
        auth={"Authorization": f"Bearer {app.state.api_token}"},
        settings=settings,
        container=container,
    )


def _closed_trade(harness: _Harness) -> tuple[dict[str, Any], dict[str, Any]]:
    container = harness.container
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunities = list(container.store.list_opportunities(state="OPEN"))
    assert opportunities
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunities[-1].opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=harness.settings, store=container.store, clock=container.clock)
    accepted = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=harness.settings,
        kill_switch=kill,
    ).accept(run.trigger.trigger_id, idempotency_key="review-e2e-entry")
    assert accepted.position is not None
    position = accepted.position
    set_position_mark(harness.settings, position["position_id"], position["stop_price"])
    closed = PositionService(
        store=container.store, clock=container.clock, settings=harness.settings
    ).monitor(position["position_id"])
    assert closed.trade_log is not None
    return position, closed.trade_log


def test_review_read_missing_generate_link_and_idempotent_replay(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]
        missing = harness.client.get(f"/api/v1/journal/{trade_id}/review", headers=harness.auth)
        headers = {**harness.auth, "Idempotency-Key": "review-e2e-1"}
        created = harness.client.post(f"/api/v1/journal/{trade_id}/review", headers=headers)
        linked = harness.client.get(f"/api/v1/journal/{trade_id}/review", headers=harness.auth)
        read = harness.client.get(f"/api/v1/journal/{trade_id}", headers=harness.auth)
        replay = harness.client.post(f"/api/v1/journal/{trade_id}/review", headers=headers)
        decisions = harness.container.store.list_decisions(role="REVIEW")
    finally:
        harness.client.close()

    assert missing.status_code == 200, missing.text
    missing_envelope = missing.json()
    _validate_envelope(missing_envelope)
    assert missing_envelope["data"]["status"] == "MISSING"
    assert missing_envelope["data"]["advisory"] is True
    assert missing_envelope["data"]["decision_id"] is None
    assert missing_envelope["data"]["reason"]

    assert created.status_code == 200, created.text
    created_envelope = created.json()
    _validate_envelope(created_envelope)
    result = created_envelope["data"]
    assert result["status"] == "FOUND"
    assert result["advisory"] is True
    assert isinstance(result["rationale"], str) and result["rationale"].strip()
    assert isinstance(result["caveats"], list) and result["caveats"]
    assert isinstance(result["confidence"], (int, float))
    assert 0.0 <= float(result["confidence"]) <= 1.0
    assert result["validation_status"] in {"VALID", "REPAIRED"}
    assert isinstance(result["provenance"], dict)
    assert result["decision_id"]
    assert result["idempotent_replay"] is False
    # No raw prompt body or credential is echoed by the response.
    body = json.dumps(created_envelope)
    assert "system_prompt" not in body
    assert "raw_prompt" not in body
    assert "secret" not in body.lower()

    assert linked.status_code == 200, linked.text
    linked_envelope = linked.json()
    _validate_envelope(linked_envelope)
    assert linked_envelope["data"]["status"] == "FOUND"
    assert linked_envelope["data"]["decision_id"] == result["decision_id"]
    assert linked_envelope["data"]["advisory"] is True

    assert read.status_code == 200, read.text
    read_envelope = read.json()
    _validate_envelope(read_envelope)
    assert read_envelope["data"]["review_decision_id"] == result["decision_id"]

    assert replay.status_code == 200, replay.text
    replay_envelope = replay.json()
    _validate_envelope(replay_envelope)
    assert replay_envelope["meta"]["idempotent_replay"] is True
    assert replay_envelope["data"]["decision_id"] == result["decision_id"]
    # Replay reuses the stored result; it never creates a second review.
    assert len(decisions) == 1
    assert decisions[0].decision_id == result["decision_id"]


def test_review_post_requires_matching_csrf_for_browser_requests(tmp_path: Path) -> None:
    """The browser path (session cookie) needs a matching CSRF header.

    A same-origin browser POST without ``X-CSRF-Token`` is rejected before any
    review work; the same request with the session's CSRF cookie echoed in the
    header persists one review, and a replay under the same key returns the
    stored result instead of creating a second review.
    """
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]
        url = f"/api/v1/journal/{trade_id}/review"

        # Mint a real browser session from the one-time bootstrap secret.
        secret = harness.app.state.browser_bootstrap
        minted = harness.client.get(f"/?bootstrap={secret}")
        assert minted.status_code == 200, minted.text
        assert harness.client.cookies.get("oft_session")
        # A cookie-setting GET issues the double-submit CSRF cookie.
        health = harness.client.get("/health")
        assert health.status_code == 200, health.text
        csrf = harness.client.cookies.get(CSRF_COOKIE)
        assert csrf
        origin = sorted(allowed_origins(harness.settings))[0]
        key = "review-e2e-csrf"

        missing = harness.client.post(url, headers={"Origin": origin, "Idempotency-Key": key})
        created = harness.client.post(
            url,
            headers={"Origin": origin, "Idempotency-Key": key, CSRF_HEADER: csrf},
        )
        replay = harness.client.post(
            url,
            headers={"Origin": origin, "Idempotency-Key": key, CSRF_HEADER: csrf},
        )
        decisions = harness.container.store.list_decisions(role="REVIEW")
    finally:
        harness.client.close()

    assert missing.status_code == 403, missing.text
    assert missing.json()["ok"] is False
    assert missing.json()["error"]["code"] == "CSRF_INVALID"

    assert created.status_code == 200, created.text
    created_envelope = created.json()
    _validate_envelope(created_envelope)
    assert created_envelope["data"]["status"] == "FOUND"
    assert created_envelope["data"]["advisory"] is True
    assert created_envelope["meta"]["idempotent_replay"] is False
    decision_id = created_envelope["data"]["decision_id"]
    assert decision_id

    assert replay.status_code == 200, replay.text
    replay_envelope = replay.json()
    _validate_envelope(replay_envelope)
    assert replay_envelope["meta"]["idempotent_replay"] is True
    assert replay_envelope["data"]["decision_id"] == decision_id
    # The rejected CSRF request created nothing, and the replay reused the one
    # persisted review rather than writing a second.
    assert len(decisions) == 1
    assert decisions[0].decision_id == decision_id


def test_review_failure_keeps_metrics_and_comparison_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    try:
        _position, trade = _closed_trade(harness)
        trade_id = trade["trade_id"]
        original = harness.container.llm_provider
        assert original is not None

        def failing_service(request: Any, provider: str | None) -> ReviewService:
            return ReviewService(
                store=harness.container.store,
                clock=harness.container.clock,
                settings=harness.settings,
                provider=_FailingProvider(original),
                runtime=harness.container.llm_runtime,
            )

        monkeypatch.setattr(journal_route, "_review_service", failing_service)
        failed = harness.client.post(
            f"/api/v1/journal/{trade_id}/review",
            headers={**harness.auth, "Idempotency-Key": "review-e2e-fail"},
        )
        detail = harness.client.get(f"/api/v1/journal/{trade_id}", headers=harness.auth)
        compare = harness.client.get(f"/api/v1/journal/compare/{trade_id}", headers=harness.auth)
        stats = harness.client.get(
            "/api/v1/journal/stats", params={"period": "8w"}, headers=harness.auth
        )
        review = harness.client.get(f"/api/v1/journal/{trade_id}/review", headers=harness.auth)
    finally:
        harness.client.close()

    assert failed.status_code >= 400, failed.text
    failure_envelope = failed.json()
    assert failure_envelope["ok"] is False
    assert failure_envelope["error"] is not None

    for response in (detail, compare, stats):
        assert response.status_code == 200, response.text
        _validate_envelope(response.json())
    assert detail.json()["data"]["trade_id"] == trade_id
    assert compare.json()["data"]["trade_id"] == trade_id
    assert stats.json()["data"]["status"] in {"DEFINED", "UNDEFINED"}
    # The failed generation never linked a review.
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "MISSING"


def test_review_sources_are_advisory_and_render_no_raw_html() -> None:
    review_source = (WEB_SRC / "components" / "review-band.tsx").read_text(encoding="utf-8")
    detail_source = (WEB_SRC / "routes" / "journal-detail.tsx").read_text(encoding="utf-8")
    api_source = (WEB_SRC / "api" / "journal.ts").read_text(encoding="utf-8")

    assert "仅建议，不自动修改规则" in review_source
    # Generate, retry after a failure, and the idempotent-replay note exist.
    for marker in ("生成复盘", "重试生成", "幂等重放"):
        assert marker in review_source, marker
    # The compact (<=768px) detail is read-only and says so.
    assert "data-readonly={compact}" in detail_source
    assert "readonly-note" in review_source
    for source in (review_source, detail_source):
        assert "dangerouslySetInnerHTML" not in source
        assert "innerHTML" not in source
        assert "javascript:" not in source
        assert "system_prompt" not in source
        assert "raw_prompt" not in source
        assert "prompt_body" not in source
        assert "api_key" not in source
        assert "secret_key" not in source
        assert 'target="_blank"' not in source
    # The band offers no apply/rule-change surface.
    assert "应用" not in review_source
    assert "apply" not in review_source.lower()
    # Review generation goes through the CSRF + idempotent API client.
    assert "Idempotency-Key" in api_source
    assert "/review" in api_source
    assert "X-CSRF-Token" in api_source
