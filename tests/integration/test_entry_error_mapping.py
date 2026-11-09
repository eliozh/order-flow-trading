"""Entry re-check/provider failures map to catalog codes, never a 500 (H2, M11, M16)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oft.adapters.llm_common import LLMAuthError, LLMRateLimited
from oft.application.entry_service import EntryRejected, EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import LLMRequest, LLMResult, LLMUsage

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
ANALYSIS_TEXT = (FIXTURES / "opportunity_valid.json").read_text(encoding="utf-8")
ENTRY_TEXT = (FIXTURES / "entry_recheck_enter_long.json").read_text(encoding="utf-8")
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 9, tzinfo=UTC)
SENTINEL = "sk-SENTINEL-DO-NOT-LEAK"


class _RoleProvider:
    provider = "openai"

    def __init__(self, *, entry_error: Exception | None = None) -> None:
        self._entry_error = entry_error
        self.calls: list[LLMRequest] = []

    def capabilities(self) -> Any:
        from oft.ports.llm import ProviderCapabilities

        return ProviderCapabilities(provider="openai", strict_schema=True)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        if request.role.value == "ENTRY_RECHECK" and self._entry_error is not None:
            raise self._entry_error
        text = ANALYSIS_TEXT if request.role.value == "ANALYSIS" else ENTRY_TEXT
        return LLMResult(
            provider="openai",
            model=request.model,
            raw_text=text,
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            request_id="req-1",
        )

    def estimate_cost(self, request: LLMRequest, model: str) -> Any:
        from oft.ports.llm import CostEstimate

        return CostEstimate(amount_usd="0", price_version="none/v1", unpriced=True)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "port": 8765,
        "data_dir": tmp_path / "data",
        "archive_fixture_dir": ARCHIVE,
        "llm_provider": "stub",
        "llm_fixtures_dir": FIXTURES,
        "llm_model_entry_recheck": "gpt-recheck",
        "openai_api_key": SENTINEL,
    }
    values.update(overrides)
    return Settings(**values)


def _trigger(tmp_path: Path, settings: Settings) -> tuple[Any, str]:
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    return container, run.trigger.trigger_id


def _entry(container: Any, settings: Settings, provider: Any) -> EntryService:
    return EntryService(
        store=container.store,
        clock=container.clock,
        provider=provider,
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=container.store, clock=container.clock),
    )


def test_a_provider_auth_failure_maps_to_llm_auth_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, trigger_id = _trigger(tmp_path, settings)

    with pytest.raises(EntryRejected) as caught:
        _entry(container, settings, _RoleProvider(entry_error=LLMAuthError("bad key"))).recheck(
            trigger_id
        )

    assert caught.value.code == "LLM_AUTH_FAILED"


def test_a_rate_limit_maps_to_llm_rate_limited(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, trigger_id = _trigger(tmp_path, settings)
    error = LLMRateLimited("rate limited", status=429)
    error.retryable = False  # avoid the backoff sleep in this assertion

    with pytest.raises(EntryRejected) as caught:
        _entry(container, settings, _RoleProvider(entry_error=error)).recheck(trigger_id)

    assert caught.value.code == "LLM_RATE_LIMITED"


def test_a_missing_role_model_maps_to_llm_model_not_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_model_entry_recheck=None)
    container, trigger_id = _trigger(tmp_path, settings)

    with pytest.raises(EntryRejected) as caught:
        _entry(container, settings, _RoleProvider()).recheck(trigger_id)

    assert caught.value.code == "LLM_MODEL_NOT_CONFIGURED"


def test_a_provider_without_a_key_makes_no_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_api_key=None)
    container, trigger_id = _trigger(tmp_path, settings)
    provider = _RoleProvider()

    with pytest.raises(EntryRejected) as caught:
        _entry(container, settings, provider).recheck(trigger_id)

    assert caught.value.code == "LLM_PROVIDER_NOT_CONFIGURED"
    assert provider.calls == []


def test_a_corrupt_price_table_degrades_only_entry_accounting(tmp_path: Path) -> None:
    """The re-check still runs; the billed call is recorded unpriced (F5)."""
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": "v", "models": []}), encoding="utf-8")
    settings = _settings(tmp_path, llm_price_table=prices)
    container, trigger_id = _trigger(tmp_path, settings)
    prices.write_text("{not json", encoding="utf-8")

    updated = _entry(container, settings, _RoleProvider()).recheck(trigger_id)

    assert updated["status"] == "RECHECKED"
    budgets = container.store.list_budget_records()
    assert budgets and all(record.unpriced for record in budgets)
    events = [
        event
        for event in container.store.list_audit_events()
        if event.event_type == "LLM_PRICE_TABLE_INVALID"
    ]
    assert events, "a corrupt price table must be surfaced as an audit event"


def test_the_recheck_route_returns_the_mapped_provider_code_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.api.app import create_app

    error = LLMRateLimited("rate limited", status=429)
    error.retryable = False
    settings = _settings(tmp_path, llm_provider="openai", llm_model_analysis="gpt-fixture")
    provider = _RoleProvider(entry_error=error)
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: provider)

    app = create_app(settings, clock=FixedClock(NOW))
    container = app.state.container
    backfill = container.backfill.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert backfill.run.status == "COMPLETE"
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False)
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None

    client = TestClient(app)
    auth = {"Authorization": f"Bearer {app.state.api_token}", "Idempotency-Key": "k-recheck"}
    try:
        response = client.post(f"/api/v1/triggers/{run.trigger.trigger_id}/recheck", headers=auth)
    finally:
        client.close()

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "LLM_RATE_LIMITED"
