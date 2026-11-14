"""Integration: trigger list provenance fields and the persisted decision route."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.adapters.sqlite_llm_ledger import LLMStoreMixin
from oft.api.app import create_app
from oft.api.routes import triggers as triggers_route
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import AppContainer, build_container
from oft.clock import FixedClock
from oft.config import ConfigError, Settings
from oft.ports.llm import LLMRequest, LLMResult, ProviderCapabilities

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = datetime(2026, 10, 1, tzinfo=UTC).date()
END = datetime(2026, 10, 7, tzinfo=UTC).date()
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


class _ExplodingProvider:
    """A provider whose completion must never run for a persisted read."""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="stub",
            strict_schema=True,
            max_output_tokens=4096,
            notes="no-call guard",
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        raise AssertionError("the decision read must not call the provider")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _setup(tmp_path: Path) -> tuple[Settings, Any, str]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    trigger_id = run.trigger.trigger_id
    service = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )
    service.recheck(trigger_id)
    return settings, container, trigger_id


def _client(settings: Settings) -> tuple[TestClient, dict[str, str]]:
    app = create_app(settings, clock=FixedClock(NOW))
    return TestClient(app), {"Authorization": f"Bearer {app.state.api_token}"}


def test_the_trigger_list_projects_environment_risk_verdict_and_confidence(
    tmp_path: Path,
) -> None:
    settings, container, trigger_id = _setup(tmp_path)
    client, auth = _client(settings)
    with client:
        response = client.get("/api/v1/triggers", headers=auth)

    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["trigger_id"] == trigger_id
    assert item["environment"] == "PAPER"
    assert isinstance(item["risk_verdict"], dict)
    assert item["risk_verdict"]["verdict"] in {"ALLOW", "REDUCE", "BLOCK"}
    assert item["confidence"] == pytest.approx(0.68)

    stored = json.loads(container.store.get_trigger_event(trigger_id).payload_json)
    # The extra projection fields are never written into the stored contract row.
    assert "confidence" not in stored
    assert stored["risk_verdict"] == item["risk_verdict"]


def test_the_decision_route_reads_the_persisted_decision_without_a_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, container, trigger_id = _setup(tmp_path)
    client, auth = _client(settings)
    guard = EntryService(
        store=container.store,
        clock=container.clock,
        provider=_ExplodingProvider(),
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )
    monkeypatch.setattr(triggers_route, "_decision_entry_service", lambda _request: guard)

    with client:
        response = client.get(f"/api/v1/triggers/{trigger_id}/decision", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["provenance"] == "MIXED"
    assert body["meta"]["data_quality"] == "COMPLETE"
    data = body["data"]
    assert data["status"] == "FOUND"
    assert data["provenance"]["provider"] == "stub"
    assert data["provenance"]["prompt_id"] == "entry_recheck"
    assert data["provenance"]["cost"]["currency"] == "USD"
    assert data["validation_status"] == "VALID"
    assert data["confidence"] == pytest.approx(0.68)
    assert data["decision_type"] == "ENTRY_RECHECK"
    assert data["decision"]["entry_recheck"]["decision"] in {
        "ENTER_LONG",
        "ENTER_SHORT",
        "NO_ENTRY",
    }


def test_a_missing_persisted_decision_is_a_missing_envelope(tmp_path: Path) -> None:
    settings, container, trigger_id = _setup(tmp_path)
    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    decision_id = json.loads(record.payload_json)["entry_decision_id"]
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute("DELETE FROM decisions WHERE decision_id = ?", (decision_id,))
        connection.commit()
    client, auth = _client(settings)

    with client:
        response = client.get(f"/api/v1/triggers/{trigger_id}/decision", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["data_quality"] == "MISSING"
    assert body["data"]["status"] == "MISSING"
    assert body["data"]["decision"] is None
    assert body["data"]["reasons"]


def test_the_api_recheck_refresh_flag_reruns_and_audits(
    tmp_path: Path,
) -> None:
    settings, container, trigger_id = _setup(tmp_path)
    client, auth = _client(settings)

    with client:
        response = client.post(
            f"/api/v1/triggers/{trigger_id}/recheck?refresh=true",
            headers={**auth, "Idempotency-Key": "api-refresh"},
        )

    assert response.status_code == 200, response.text
    card = response.json()["data"]
    assert card["refreshed"] is True
    assert card["llm"]["provenance"]["provider"] == "stub"
    audits = [
        event
        for event in container.store.list_audit_events(limit=1000)
        if event.event_type == "ENTRY_RECHECK_REFRESHED"
    ]
    assert audits


def test_the_trigger_list_batches_the_decision_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _container, trigger_id = _setup(tmp_path)
    client, auth = _client(settings)

    def explode(_store: Any, _decision_id: str) -> Any:
        raise AssertionError("the list projection must not issue one read per item")

    monkeypatch.setattr(LLMStoreMixin, "get_decision", explode)
    with client:
        response = client.get("/api/v1/triggers", headers=auth)

    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["trigger_id"] == trigger_id
    assert item["confidence"] == pytest.approx(0.68)


def test_the_trigger_list_handles_missing_decisions_and_an_empty_page(
    tmp_path: Path,
) -> None:
    settings, container, trigger_id = _setup(tmp_path)
    record = container.store.get_trigger_event(trigger_id)
    assert record is not None
    decision_id = json.loads(record.payload_json)["entry_decision_id"]
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute("DELETE FROM decisions WHERE decision_id = ?", (decision_id,))
        connection.commit()
    client, auth = _client(settings)

    with client:
        mixed = client.get("/api/v1/triggers", headers=auth)
        empty = client.get("/api/v1/triggers?status=EXPIRED", headers=auth)

    assert mixed.status_code == 200, mixed.text
    assert mixed.json()["data"]["items"][0]["confidence"] is None
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["items"] == []
    assert empty.json()["data"]["total"] == 0


def test_a_corrupt_trigger_payload_is_a_missing_decision_not_a_500(
    tmp_path: Path,
) -> None:
    settings, _container, trigger_id = _setup(tmp_path)
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute(
            "UPDATE trigger_events SET payload_json = ? WHERE trigger_id = ?",
            ("{not-json", trigger_id),
        )
        connection.commit()
    client, auth = _client(settings)

    with client:
        response = client.get(f"/api/v1/triggers/{trigger_id}/decision", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["status"] == "MISSING"
    assert body["data"]["decision"] is None
    assert "unreadable" in body["data"]["reasons"][0]
    assert body["meta"]["data_quality"] == "MISSING"


def test_the_decision_route_never_resolves_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _container, trigger_id = _setup(tmp_path)
    client, auth = _client(settings)

    def unavailable(self: Any, requested: str | None = None) -> Any:
        raise ConfigError("the provider key was removed")

    monkeypatch.setattr(AppContainer, "provider_for", unavailable)
    with client:
        response = client.get(f"/api/v1/triggers/{trigger_id}/decision", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "FOUND"


def test_an_unknown_trigger_decision_is_a_404_through_the_real_route(
    tmp_path: Path,
) -> None:
    settings, _container, _trigger_id = _setup(tmp_path)
    client, auth = _client(settings)

    with client:
        response = client.get("/api/v1/triggers/TRG-MISSING/decision", headers=auth)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
