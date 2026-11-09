"""Capability probes, provider status chips, and D10 settings validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from oft.adapters.llm_deepseek import DeepSeekAdapter
from oft.adapters.llm_openai import OpenAIAdapter
from oft.adapters.sqlite_store import SqliteStore
from oft.application.llm_orchestrator import (
    CapabilityProbeService,
    LLMConfigurationError,
    capability_status,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.llm import ProbeOutcome

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class _FakeAdapter:
    def __init__(self, outcome: ProbeOutcome) -> None:
        self.outcome = outcome
        self.models: list[str] = []

    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        self.models.append(model)
        return self.outcome


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "port": 8765,
        "data_dir": tmp_path / "data",
        "openai_api_key": "sk-SENTINEL-DO-NOT-LEAK",
        "llm_provider": "openai",
        "llm_model_analysis": "gpt-fixture",
    }
    values.update(overrides)
    return Settings(**values)


def _store(settings: Settings) -> SqliteStore:
    store = SqliteStore(settings)
    store.initialize()
    return store


def _service(tmp_path: Path, outcome: ProbeOutcome, **overrides: Any) -> Any:
    settings = _settings(tmp_path, **overrides)
    store = _store(settings)
    adapter = _FakeAdapter(outcome)
    service = CapabilityProbeService(
        settings=settings,
        store=store,
        clock=FixedClock(NOW),
        adapter_factory=lambda _settings, _name: adapter,
    )
    return service, store, adapter


def test_a_strict_probe_is_persisted_with_the_strict_chip(tmp_path: Path) -> None:
    service, store, adapter = _service(
        tmp_path, ProbeOutcome(strict_schema=True, notes="strict schema accepted", latency_ms=12)
    )

    record = service.run("openai")

    assert record.status == "OK"
    assert record.strict_schema is True
    assert record.degraded is False
    assert record.checked_at == "2026-11-09T12:00:00.000Z"
    assert adapter.models == ["gpt-fixture"]
    assert store.get_provider_capability("openai") == record
    assert capability_status(record) == "STRICT"


def test_a_degraded_probe_shows_the_degraded_chip(tmp_path: Path) -> None:
    service, _store_, _adapter = _service(
        tmp_path,
        ProbeOutcome(strict_schema=False, degraded=True, notes="degraded: JSON mode fallback"),
    )

    record = service.run("openai")

    assert record.status == "OK"
    assert capability_status(record) == "DEGRADED"


def test_a_failed_probe_is_recorded_not_raised(tmp_path: Path) -> None:
    service, store, _adapter = _service(
        tmp_path,
        ProbeOutcome(
            strict_schema=False,
            error_code="LLM_AUTH_FAILED",
            notes="the provider rejected the API key",
        ),
    )

    record = service.run("openai")

    assert record.status == "FAILED"
    assert record.error_code == "LLM_AUTH_FAILED"
    assert capability_status(record) == "FAILED"
    assert store.get_provider_capability("openai") is not None


def test_unchecked_when_no_row_exists() -> None:
    assert capability_status(None) == "UNCHECKED"


def test_a_missing_key_is_provider_not_configured(tmp_path: Path) -> None:
    service, _store_, _adapter = _service(
        tmp_path, ProbeOutcome(strict_schema=True), openai_api_key=None
    )

    with pytest.raises(LLMConfigurationError) as caught:
        service.run("openai")

    assert caught.value.code == "LLM_PROVIDER_NOT_CONFIGURED"
    assert "sk-" not in caught.value.message


def test_an_unset_role_model_is_model_not_configured(tmp_path: Path) -> None:
    service, _store_, _adapter = _service(
        tmp_path, ProbeOutcome(strict_schema=True), llm_model_analysis=None
    )

    with pytest.raises(LLMConfigurationError) as caught:
        service.run("openai", role="ANALYSIS")

    assert caught.value.code == "LLM_MODEL_NOT_CONFIGURED"


def test_an_unknown_provider_is_provider_not_configured(tmp_path: Path) -> None:
    service, _store_, _adapter = _service(tmp_path, ProbeOutcome(strict_schema=True))

    with pytest.raises(LLMConfigurationError) as caught:
        service.run("mystery")

    assert caught.value.code == "LLM_PROVIDER_NOT_CONFIGURED"


def test_settings_ship_no_default_model_and_the_adr_defaults(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")

    assert settings.llm_model_analysis is None
    assert settings.llm_model_entry_recheck is None
    assert settings.llm_max_concurrency == 2
    assert settings.llm_max_concurrency_deepseek == 1
    assert settings.llm_openai_base_url == "https://api.openai.com"
    assert settings.llm_anthropic_base_url == "https://api.anthropic.com"
    assert settings.llm_deepseek_base_url == "https://api.deepseek.com"
    assert settings.llm_price_table is None
    assert settings.budget_daily_usd == "10.00"
    assert settings.budget_weekly_usd == "50.00"
    assert settings.budget_monthly_usd == "150.00"
    assert settings.budget_warn_pct == 80.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"budget_daily_usd": "0"},
        {"budget_daily_usd": "-1"},
        {"budget_daily_usd": "oops"},
        {"budget_warn_pct": 0},
        {"budget_warn_pct": 101},
        {"llm_max_concurrency": 0},
        {"llm_max_concurrency_deepseek": 0},
    ],
)
def test_invalid_llm_and_budget_settings_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(env="test", **overrides)


def test_price_table_path_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "prices.json").write_text(
        json.dumps({"version": "v", "models": []}), encoding="utf-8"
    )

    settings = Settings(env="test", llm_price_table=Path("~/prices.json"))

    assert settings.llm_price_table == tmp_path / "prices.json"


def test_an_empty_price_table_placeholder_is_unset() -> None:
    settings = Settings(env="test", llm_price_table="")

    assert settings.llm_price_table is None


def test_an_invalid_price_table_is_config_invalid(tmp_path: Path) -> None:
    from oft.config import ConfigError, load_settings

    bad = tmp_path / "bad-prices.json"
    bad.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValidationError):
        Settings(env="test", llm_price_table=bad)
    with pytest.raises(ConfigError) as caught:
        load_settings(llm_price_table=bad)
    assert caught.value.code == "CONFIG_INVALID"


def test_deepseek_probe_falls_back_to_json_mode(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "tools[0].function.parameters: strict schema is not supported",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "ds_probe",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"ok": "ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    adapter = DeepSeekAdapter(
        api_key="sk-SENTINEL-DO-NOT-LEAK",
        base_url="https://api.deepseek.test",
        transport=httpx.MockTransport(handler),
    )

    outcome = adapter.probe(model="deepseek-fixture")

    assert outcome.strict_schema is False
    assert outcome.degraded is True
    assert outcome.error_code is None
    assert outcome.notes == "degraded: JSON mode fallback"
    assert "tools" in calls[0]
    assert calls[1]["response_format"] == {"type": "json_object"}


def test_openai_probe_records_strict_success() -> None:
    body = {
        "id": "resp_probe",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"ok": "ok"}'}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    adapter = OpenAIAdapter(
        api_key="sk-SENTINEL-DO-NOT-LEAK",
        base_url="https://api.openai.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )

    outcome = adapter.probe(model="gpt-fixture")

    assert outcome.strict_schema is True
    assert outcome.degraded is False
    assert outcome.error_code is None
    # The advertised capability, not the 64-token request cap (M9).
    assert outcome.max_output_tokens == adapter.capabilities().max_output_tokens
    assert outcome.max_output_tokens != 64
