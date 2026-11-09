"""CLI surfaces: oft llm providers|models|test|usage and oft budget show|set|usage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.adapters.sqlite_store import database_path
from oft.cli.main import app
from oft.config import Settings
from oft.ports.llm import ProbeOutcome

SENTINEL = "sk-SENTINEL-DO-NOT-LEAK"


class _StrictAdapter:
    def probe(self, *, model: str, role: Any = None) -> ProbeOutcome:
        return ProbeOutcome(strict_schema=True, notes="strict schema accepted", latency_ms=3)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "port": 8765,
        "data_dir": tmp_path / "data",
        "llm_provider": "openai",
        "llm_model_analysis": "gpt-fixture",
        "openai_api_key": SENTINEL,
    }
    values.update(overrides)
    settings = Settings(**values)
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    monkeypatch.setenv("OFT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OFT_LLM_MODEL_ANALYSIS", "gpt-fixture")
    monkeypatch.setenv("OFT_OPENAI_API_KEY", SENTINEL)
    if settings.llm_price_table is not None:
        monkeypatch.setenv("OFT_LLM_PRICE_TABLE", str(settings.llm_price_table))
    return settings


def test_providers_lists_the_three_providers_and_never_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["llm", "providers", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    items = {item["provider"]: item for item in payload["data"]["items"]}
    assert set(items) == {"openai", "anthropic", "deepseek"}
    assert items["openai"]["configured"] is True
    assert items["anthropic"]["configured"] is False
    assert items["openai"]["capability"] is None
    assert items["openai"]["roles"]["ANALYSIS"] == "gpt-fixture"
    assert SENTINEL not in result.stdout


def test_models_lists_priced_rows_and_unpriced_role_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "version": "2026-11-09",
                "models": [
                    {
                        "provider": "openai",
                        "model": "gpt-priced",
                        "input_per_mtok": "1.00",
                        "output_per_mtok": "2.00",
                        "cache_read_per_mtok": "0.50",
                        "cache_write_per_mtok": "0.75",
                        "source_url": "",
                        "as_of": "2026-11-09",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _configure(monkeypatch, tmp_path, llm_price_table=prices)

    result = CliRunner().invoke(app, ["llm", "models", "--provider", "openai", "--json"])

    assert result.exit_code == 0, result.output
    items = {item["model"]: item for item in json.loads(result.stdout)["data"]["items"]}
    assert items["gpt-priced"]["unpriced"] is False
    assert items["gpt-fixture"]["unpriced"] is True


def test_llm_test_records_the_capability_and_a_sentinel_never_reaches_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: _StrictAdapter())

    result = CliRunner().invoke(
        app, ["llm", "test", "--provider", "openai", "--role", "ANALYSIS", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "STRICT"
    assert payload["data"]["strict_schema"] is True
    assert SENTINEL not in result.stdout
    assert SENTINEL.encode() not in database_path(settings).read_bytes()


def test_llm_test_without_a_key_exits_with_the_catalog_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv("OFT_OPENAI_API_KEY")
    monkeypatch.setattr("oft.bootstrap.build_llm_adapter", lambda *_: _StrictAdapter())

    result = CliRunner().invoke(app, ["llm", "test", "--provider", "openai", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "LLM_PROVIDER_NOT_CONFIGURED"


def test_llm_usage_reports_zero_spend_on_a_clean_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["llm", "usage", "--period", "day", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["spend_usd"] == "0.00000000"
    assert data["call_count"] == 0
    assert data["unpriced_count"] == 0
    assert data["thresholds"]["daily_usd"] == "10.00"


def test_budget_show_set_and_usage_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    runner = CliRunner()

    shown = runner.invoke(app, ["budget", "show", "--json"])
    updated = runner.invoke(app, ["budget", "set", "--daily", "5.00", "--json"])
    after = runner.invoke(app, ["budget", "show", "--json"])
    usage = runner.invoke(app, ["budget", "usage", "--period", "day", "--json"])

    assert shown.exit_code == 0, shown.output
    assert shown.stdout and json.loads(shown.stdout)["data"]["thresholds"]["daily_usd"] == "10.00"
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.stdout)["data"]["thresholds"]["daily_usd"] == "5.00"
    assert json.loads(after.stdout)["data"]["thresholds"]["daily_usd"] == "5.00"
    assert usage.exit_code == 0, usage.output
    assert json.loads(usage.stdout)["data"]["period"] == "day"


def test_budget_set_requires_at_least_one_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["budget", "set", "--json"])

    assert result.exit_code == 2
    assert "at least one" in result.stdout


def test_budget_set_rejects_a_non_positive_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["budget", "set", "--daily", "0", "--json"])

    assert result.exit_code == 2
    assert "positive" in result.stdout.lower()


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def test_llm_usage_maps_a_store_failure_to_its_catalog_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from oft.ports.store import StoreUnavailableError

    class _BoomService:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def usage(self, period: str, *, at: str) -> dict[str, Any]:
            raise StoreUnavailableError("the ledger is unreadable")

    logger = _RecordingLogger()
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr("oft.cli.commands.llm.BudgetService", _BoomService)
    monkeypatch.setattr("oft.cli.commands.llm.LOGGER", logger)

    result = CliRunner().invoke(app, ["llm", "usage", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "DATA_UNAVAILABLE"
    assert logger.events and logger.events[0][1]["code"] == "DATA_UNAVAILABLE"


def test_llm_usage_maps_a_config_error_to_validation_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from oft.application.budget_service import BudgetConfigError

    class _BoomService:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def usage(self, period: str, *, at: str) -> dict[str, Any]:
            raise BudgetConfigError("period must be day, week, or month")

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr("oft.cli.commands.llm.BudgetService", _BoomService)

    result = CliRunner().invoke(app, ["llm", "usage", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "VALIDATION_FAILED"
