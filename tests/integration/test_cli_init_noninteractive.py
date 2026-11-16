"""``oft init --non-interactive``: deterministic flags, no prompts, no partial state.

The required flags are derived from the actual missing prerequisite of each
incomplete active step, validated before any write, and a missing flag exits 2
without recording a completed step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.application.budget_service import BudgetService
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.cli.commands import init as init_module
from oft.cli.main import app
from oft.config import ConfigError, Settings, load_settings
from oft.domain.llm_settings import MAX_API_KEY_LENGTH
from oft.ports.store import StoreUnavailableError

SENTINEL = "sk-init-stdin-SENTINEL-9b71"
ROTATED_SENTINEL = "sk-init-rotated-SENTINEL-2e18"


class _MemoryBackend:
    kind = "encrypted_file"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: str | None = "openai",
    with_key: bool = True,
) -> tuple[Settings, _MemoryBackend]:
    from oft.application import secrets_service as secrets_module

    memory = _MemoryBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    for name in ("OPENAI", "ANTHROPIC", "DEEPSEEK"):
        monkeypatch.delenv(f"OFT_{name}_API_KEY", raising=False)
    monkeypatch.delenv("OFT_LLM_PROVIDER", raising=False)

    values: dict[str, Any] = {"env": "test", "port": 8765, "data_dir": tmp_path / "data"}
    if provider is not None:
        values["llm_provider"] = provider
        monkeypatch.setenv("OFT_LLM_PROVIDER", provider)
        if with_key:
            values[f"{provider}_api_key"] = SENTINEL
            monkeypatch.setenv(f"OFT_{provider.upper()}_API_KEY", SENTINEL)
    settings = Settings(**values)
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(settings.data_dir))
    return settings, memory


def _progress(settings: Settings) -> list[int]:
    store = SqliteStore(settings)
    store.initialize()
    record = store.get_config("onboarding.progress")
    if record is None:
        return []
    return list(json.loads(record.value_json)["completed_steps"])


def _service(settings: Settings) -> tuple[Any, SettingsService]:
    container = build_container(settings, initialize=True)
    service = SettingsService(
        store=container.store,
        clock=container.clock,
        settings=container.llm_base_settings or settings,
    )
    return container, service


def _run(arguments: list[str], *, input_text: str | None = None):
    return CliRunner().invoke(app, ["init", "--non-interactive", *arguments], input=input_text)


def _capture_errors(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every ``emit_error`` code/message pair while still printing it."""
    calls: list[tuple[str, str]] = []
    real_emit_error = init_module.emit_error

    def _spy(
        code: str,
        message: str,
        *,
        json_output: bool,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        calls.append((code, message))
        real_emit_error(code, message, json_output=json_output, details=details)

    monkeypatch.setattr(init_module, "emit_error", _spy)
    return calls


def test_all_flags_succeed_without_prompts_and_persist_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT,XAUUSDT",
            "--scan-period",
            "8h",
            "--budget-daily",
            "5.00",
            "--budget-weekly",
            "25.00",
        ]
    )

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 5]
    container, service = _service(settings)
    scan = service.read_scan_settings()
    assert scan.value == "8h"
    assert scan.source.value == "user"
    thresholds = BudgetService(store=container.store, settings=container.settings).thresholds()
    assert str(thresholds.daily_usd) == "5.00"
    assert str(thresholds.weekly_usd) == "25.00"
    starred = {contract.symbol for contract in container.symbol_registry.list_starred()}
    assert starred == {"BTCUSDT", "XAUUSDT"}


def test_a_missing_llm_provider_exits_2_and_writes_no_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No provider key in the environment: the flag is genuinely required.
    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)

    result = _run(["--watchlist", "BTCUSDT"])

    assert result.exit_code == 2, result.output
    assert "--llm-provider" in result.output
    assert _progress(settings) == []


def test_a_provider_without_a_key_requires_llm_key_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)

    result = _run(["--llm-provider", "anthropic", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 2, result.output
    assert "--llm-key-stdin" in result.output
    assert _progress(settings) == []


def test_a_missing_watchlist_exits_2_when_nothing_is_starred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai"])

    assert result.exit_code == 2, result.output
    assert "--watchlist" in result.output
    assert _progress(settings) == []


def test_every_missing_flag_is_named_and_no_step_is_marked_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)

    result = _run([])

    assert result.exit_code == 2, result.output
    assert "--llm-provider" in result.output
    assert "--watchlist" in result.output
    assert _progress(settings) == []
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None


def test_key_from_stdin_is_stored_and_never_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path, with_key=False)

    result = _run(
        ["--llm-provider", "openai", "--llm-key-stdin", "--watchlist", "BTCUSDT"],
        input_text=f"{SENTINEL}\n",
    )

    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    assert "****9b71" in result.stdout
    assert backend.values["llm.openai.api_key"] == SENTINEL
    assert _progress(settings) == [1, 2, 5]
    # The raw persisted progress document carries no key material.
    store = SqliteStore(settings)
    store.initialize()
    progress_row = store.get_config("onboarding.progress")
    assert progress_row is not None
    assert SENTINEL not in progress_row.value_json


def test_an_overlong_stdin_key_exits_2_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)

    result = _run(
        ["--llm-provider", "openai", "--llm-key-stdin", "--watchlist", "BTCUSDT"],
        input_text="k" * (MAX_API_KEY_LENGTH + 3) + "\n",
    )

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    assert store.get_secret_meta("openai", "api_key") is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_deferred_steps_are_explicit_and_do_not_block_a_paper_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 0, result.output
    assert "Milestone 5" in result.stdout
    assert "Milestone 6" in result.stdout
    assert "Environment: test." in result.stdout
    assert "Live is not offered" in result.stdout


def test_an_unknown_symbol_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "NOPEUSDT"])

    assert result.exit_code == 2, result.output
    assert "NOPEUSDT" in result.output
    assert _progress(settings) == []


def test_a_blank_watchlist_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", ""])

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []


def test_an_invalid_scan_period_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT", "--scan-period", "1h"])

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []


def test_an_invalid_budget_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT", "--budget-daily", "0"])

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []


def test_crossed_budget_exits_2_before_stars_or_scan_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The apply path enforces ordering; pre-validation must match it exactly."""
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--scan-period",
            "8h",
            "--budget-daily",
            "30",
            "--budget-weekly",
            "10",
        ]
    )

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    assert store.get_budget_settings() is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


@pytest.mark.parametrize("daily", ["1e999999999999999999", "1e1000000", "1e-1000000", "1000000000"])
def test_an_exponent_or_out_of_range_budget_exits_2_before_any_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, daily: str
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--scan-period",
            "8h",
            "--budget-daily",
            daily,
        ]
    )

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    assert store.get_budget_settings() is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_an_unknown_provider_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "bogus", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []


def test_a_blank_symbol_in_the_watchlist_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT,"])

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_an_unavailable_secret_store_exits_5_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from oft.application import secrets_service as secrets_module

    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: None)

    result = _run(
        ["--llm-provider", "openai", "--llm-key-stdin", "--watchlist", "BTCUSDT"],
        input_text=f"{SENTINEL}\n",
    )

    assert result.exit_code == 5, result.output
    assert SENTINEL not in result.output
    assert _progress(settings) == []
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    assert store.get_secret_meta("openai", "api_key") is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_a_config_error_from_settings_load_exits_5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    def _invalid_settings() -> Settings:
        raise ConfigError("settings could not be loaded")

    monkeypatch.setattr(init_module, "load_settings", _invalid_settings)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 5, result.output
    assert "settings could not be loaded" in result.output


def test_resume_only_requires_flags_for_the_incomplete_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    container, service = _service(settings)
    service.complete_onboarding_step(1, actor="cli")
    service.complete_onboarding_step(2, actor="cli")
    container.symbol_registry.set_star_with_audit("ETHUSDT", True, actor="cli")

    # Step 2 is complete, so --llm-provider is not required; step 5's star
    # prerequisite is already satisfied, so --watchlist is optional too.
    result = _run(["--resume", "--scan-period", "4h"])

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 5]


def test_all_complete_non_interactive_resume_needs_no_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    container, service = _service(settings)
    container.symbol_registry.set_star_with_audit("BTCUSDT", True, actor="cli")
    for step in (1, 2, 5):
        service.complete_onboarding_step(step, actor="cli")

    result = _run(["--resume"])

    assert result.exit_code == 0, result.output
    assert "Completed steps: 1, 2, 5" in result.stdout
    assert _progress(settings) == [1, 2, 5]


def test_a_configured_env_key_satisfies_the_llm_prerequisite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No --llm-provider is required when a provider key is already configured."""
    settings, _ = _configure(monkeypatch, tmp_path)
    # Keep only the key in the environment: the provider is selected by init.
    monkeypatch.delenv("OFT_LLM_PROVIDER", raising=False)

    result = _run(["--watchlist", "BTCUSDT"])

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 5]
    _container, service = _service(load_settings())
    assert service.read_effective_llm().provider == "openai"


def test_llm_key_stdin_without_a_provider_exits_2_before_reading_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unattributable key is rejected rather than silently dropped."""
    settings, backend = _configure(monkeypatch, tmp_path)
    # Only the env key is configured: no provider is named by flag or env.
    monkeypatch.delenv("OFT_LLM_PROVIDER", raising=False)

    def _unexpected_read() -> str:
        raise AssertionError("stdin must not be read")

    monkeypatch.setattr(init_module, "_read_secret_key", _unexpected_read)
    calls = _capture_errors(monkeypatch)

    result = _run(
        ["--llm-key-stdin", "--watchlist", "BTCUSDT"],
        input_text=f"{ROTATED_SENTINEL}\n",
    )

    assert result.exit_code == 2, result.output
    assert "requires --llm-provider" in result.output
    assert ROTATED_SENTINEL not in result.output
    # The usage error precedes settings load, so nothing was written at all.
    assert calls[-1][0] == "VALIDATION_FAILED"
    assert backend.values == {}
    assert not settings.data_dir.exists()


def test_a_named_provider_with_llm_key_stdin_rotates_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    result = _run(
        ["--llm-provider", "openai", "--llm-key-stdin", "--watchlist", "BTCUSDT"],
        input_text=f"{ROTATED_SENTINEL}\n",
    )

    assert result.exit_code == 0, result.output
    assert ROTATED_SENTINEL not in result.output
    assert backend.values["llm.openai.api_key"] == ROTATED_SENTINEL
    assert _progress(settings) == [1, 2, 5]


def test_a_live_environment_is_reported_and_warned_about(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_ENV", "live")

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 0, result.output
    assert "environment     live" in result.stdout
    assert "Environment: live." in result.stdout
    assert "PAPER" not in result.stdout
    assert "no Live configuration is written" in result.output


def test_a_data_dir_under_a_file_exits_5_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    data_dir = blocker / "data"
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    calls = _capture_errors(monkeypatch)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 5, result.output
    assert "Traceback" not in result.output
    assert str(data_dir) in result.output
    assert "oft doctor" in result.output
    assert calls[-1][0] == "CONFIG_INVALID"


def test_a_corrupt_database_exits_1_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "oft.db").write_bytes(b"not a sqlite database")
    calls = _capture_errors(monkeypatch)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "oft.db" in result.output
    assert "oft doctor" in result.output
    assert calls[-1][0] == "DATA_UNAVAILABLE"


def test_a_malformed_exchange_info_fixture_exits_1_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    fixture = tmp_path / "exchange-info.json"
    fixture.write_text('{"symbols": "not-an-array"}')
    monkeypatch.setenv("OFT_EXCHANGE_INFO_FIXTURE", str(fixture))
    calls = _capture_errors(monkeypatch)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "exchangeInfo" in result.output
    # ExchangeInfoParseError carries its own stable code.
    assert calls[-1][0] == "SCHEMA_INVALID"


def test_a_store_unavailable_error_exits_1_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    def _unavailable(_self: SqliteStore, _symbol: str) -> None:
        raise StoreUnavailableError("store read failed")

    monkeypatch.setattr(SqliteStore, "get_symbol", _unavailable)
    calls = _capture_errors(monkeypatch)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert calls[-1] == ("DATA_UNAVAILABLE", "store read failed")


def test_an_ansi_watchlist_token_is_sanitized_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    payload = "\x1b]0;pwned\x07BTCUSDT"

    result = _run(["--llm-provider", "openai", "--watchlist", payload])

    assert result.exit_code == 2, result.output
    combined = result.stdout + result.output
    assert "\x1b" not in combined
    assert "\x07" not in combined
    assert payload not in combined
    assert _progress(settings) == []
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_the_watchlist_error_caps_the_reported_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    payload = ",".join(f"bad-symbol-{index}" for index in range(12))

    result = _run(["--llm-provider", "openai", "--watchlist", payload])

    assert result.exit_code == 2, result.output
    assert "(+2 more)" in result.output
    assert "bad-symbol-11" not in result.output
