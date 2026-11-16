"""``oft init`` interactive, resume, secret-entry, and interrupt behaviour.

Every test runs with the daemon down: the command talks to the local stores and
services directly. The developer's OS keyring is never touched; a memory
backend stands in for the encrypted-file store.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from oft.adapters.sqlite_store import SqliteStore
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.cli.commands import init as init_module
from oft.cli.main import app
from oft.config import Settings

SENTINEL = "sk-init-SENTINEL-4f2a"


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
    **overrides: Any,
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
    values.update(overrides)
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


def _as_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)


def test_fresh_interactive_run_walks_steps_and_persists_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _as_tty(monkeypatch)

    result = CliRunner().invoke(app, ["init"], input="BTCUSDT\n\n\n")

    assert result.exit_code == 0, result.output
    assert "1/5 " in result.stdout
    assert "data directory" in result.stdout
    assert "2/5 LLM providers" in result.stdout
    assert "PAPER" in result.stdout
    assert "Milestone 5" in result.output
    assert "Milestone 6" in result.output
    assert _progress(settings) == [1, 2, 5]
    # Existing values are reported with their provenance, never guessed.
    assert "source" in result.stdout.lower()


def test_resume_starts_at_the_first_incomplete_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _container, service = _service(settings)
    service.complete_onboarding_step(1, actor="cli")
    _as_tty(monkeypatch)

    result = CliRunner().invoke(app, ["init", "--resume"], input="BTCUSDT\n\n\n")

    assert result.exit_code == 0, result.output
    assert "Resuming at step 2" in result.stdout
    assert _progress(settings) == [1, 2, 5]


def test_resume_with_all_active_steps_complete_prints_the_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    container, service = _service(settings)
    container.symbol_registry.set_star_with_audit("BTCUSDT", True, actor="cli")
    for step in (1, 2, 5):
        service.complete_onboarding_step(step, actor="cli")
    _as_tty(monkeypatch)

    result = CliRunner().invoke(app, ["init", "--resume"])

    assert result.exit_code == 0, result.output
    assert "Completed steps: 1, 2, 5" in result.stdout
    assert "PAPER" in result.stdout
    assert "Live is not offered" in result.stdout
    assert _progress(settings) == [1, 2, 5]


def test_key_entry_reads_stdin_without_echo_and_reports_only_the_mask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path, with_key=False)
    _as_tty(monkeypatch)
    prompts: list[str] = []

    def _hidden_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return SENTINEL

    monkeypatch.setattr(init_module.getpass, "getpass", _hidden_getpass)
    runner = CliRunner()

    # The provider prompt comes from stdin; the key comes from getpass, which
    # never echoes and never reads the visible input stream.
    result = runner.invoke(app, ["init"], input="openai\nBTCUSDT\n\n\n")

    assert result.exit_code == 0, result.output
    assert prompts and "input hidden" in prompts[0]
    assert SENTINEL not in result.output
    assert SENTINEL[:10] not in result.output
    assert "****4f2a" in result.stdout
    assert backend.values["llm.openai.api_key"] == SENTINEL
    # The raw persisted progress document carries no key material.
    store = SqliteStore(settings)
    store.initialize()
    progress_row = store.get_config("onboarding.progress")
    assert progress_row is not None
    assert SENTINEL not in progress_row.value_json

    providers = runner.invoke(app, ["llm", "providers", "--json"])
    assert providers.exit_code == 0, providers.output
    assert SENTINEL not in providers.stdout
    row = next(
        item
        for item in json.loads(providers.stdout)["data"]["items"]
        if item["provider"] == "openai"
    )
    assert row["masked"] == "****4f2a"


def test_init_accepts_no_value_taking_key_flag() -> None:
    help_text = CliRunner().invoke(app, ["init", "--help"]).stdout

    assert "--llm-key-stdin" in help_text
    assert re.search(r"--llm-key(?!-stdin)", help_text) is None
    # The key is a boolean stdin switch, never a value option.
    import inspect

    parameters = inspect.signature(init_module.init_command).parameters
    assert "llm_key_stdin" in parameters
    assert "llm_key" not in parameters


def test_non_tty_interactive_without_flags_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: False)

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 2, result.output
    assert "--non-interactive" in result.output
    assert "not a TTY" in result.output


def test_getpass_eof_aborts_with_a_resume_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl-D at the hidden key prompt is resumable, not a traceback."""
    settings, _ = _configure(monkeypatch, tmp_path, with_key=False)
    _as_tty(monkeypatch)

    def _eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(init_module.getpass, "getpass", _eof)

    result = CliRunner().invoke(app, ["init"], input="openai\n")

    assert result.exit_code == 130, result.output
    assert "--resume" in result.output
    assert _progress(settings) == [1]


@pytest.mark.parametrize("abort", [KeyboardInterrupt, typer.Abort, EOFError])
def test_prompt_abort_leaves_resumable_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, abort: type[BaseException]
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _as_tty(monkeypatch)

    def _interrupt(*_args: object, **_kwargs: object) -> str:
        raise abort

    monkeypatch.setattr(init_module, "_prompt_text", _interrupt)

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 130, result.output
    assert "--resume" in result.output
    # Steps completed before the interrupt are persisted; step 5 is not a
    # partial "finished" state.
    assert _progress(settings) == [1, 2]


def test_exhausted_stdin_aborts_through_the_real_prompt_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Click/Typer convert EOF at a prompt to Abort; the hint must still print."""
    settings, _ = _configure(monkeypatch, tmp_path)
    _as_tty(monkeypatch)

    result = CliRunner().invoke(app, ["init"], input="")

    assert result.exit_code == 130, result.output
    assert "--resume" in result.output
    assert _progress(settings) == [1, 2]


def test_crossed_interactive_budget_exits_2_before_step_5_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _as_tty(monkeypatch)

    # A key is already configured, so the first two prompts are the watchlist,
    # scan period, and budget; daily > weekly crosses the ordering rule.
    result = CliRunner().invoke(app, ["init"], input="BTCUSDT\n8h\n30 10 150\n")

    assert result.exit_code == 2, result.output
    assert _progress(settings) == [1, 2]
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    assert store.get_budget_settings() is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()


def test_malformed_interactive_budget_exits_2_before_step_5_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _as_tty(monkeypatch)

    result = CliRunner().invoke(app, ["init"], input="BTCUSDT\n\n10 20\n")

    assert result.exit_code == 2, result.output
    assert _progress(settings) == [1, 2]
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("scan.period") is None
    container, _ = _service(settings)
    assert container.symbol_registry.list_starred() == ()
