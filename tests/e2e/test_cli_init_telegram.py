"""``oft init`` Telegram step and ``oft telegram toggles`` (slice 53, F12/S01.4).

The bot token is read from stdin only (never argv), every output renders the
mask, and the optional Telegram step never blocks a Paper finish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fakes.telegram import MemorySecretBackend
from oft.adapters.sqlite_store import SqliteStore
from oft.application import secrets_service as secrets_module
from oft.application.settings_service import ONBOARDING_PROGRESS_KEY, SettingsService
from oft.application.telegram_service import TelegramService
from oft.bootstrap import build_container
from oft.cli.commands import init as init_module
from oft.cli.main import app
from oft.config import Settings
from oft.ports.store import ConfigRecord
from oft.ports.telegram import TelegramError

SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
OTHER = "987654321:BBOtherSentinelToken_abcdefghijklmnopqrstuv"


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    with_provider_key: bool = True,
) -> tuple[Settings, MemorySecretBackend]:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    for name in ("OPENAI", "ANTHROPIC", "DEEPSEEK"):
        monkeypatch.delenv(f"OFT_{name}_API_KEY", raising=False)
    monkeypatch.delenv("OFT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OFT_TELEGRAM_BOT_TOKEN", raising=False)

    values: dict[str, Any] = {"env": "test", "port": 8765, "data_dir": tmp_path / "data"}
    if with_provider_key:
        values.update({"llm_provider": "openai", "openai_api_key": "sk-test-telegram-init"})
        monkeypatch.setenv("OFT_LLM_PROVIDER", "openai")
        monkeypatch.setenv("OFT_OPENAI_API_KEY", "sk-test-telegram-init")
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


def _service(settings: Settings) -> Any:
    container = build_container(settings, initialize=True)
    return SettingsService(
        store=container.store,
        clock=container.clock,
        settings=container.llm_base_settings or settings,
    )


def _run(arguments: list[str], *, input_text: str | None = None) -> Any:
    return CliRunner().invoke(app, ["init", "--non-interactive", *arguments], input=input_text)


def _seed_required_complete(settings: Settings) -> None:
    """Persist progress ``[1, 2, 5]`` so only the optional step 4 remains."""
    store = SqliteStore(settings)
    store.initialize()
    store.set_config(
        ConfigRecord(
            key=ONBOARDING_PROGRESS_KEY,
            value_json=json.dumps({"completed_steps": [1, 2, 5]}),
            updated_at="2026-11-18T10:00:00.000Z",
            updated_by="test",
        )
    )


def _interactive(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    *,
    input_text: str,
) -> Any:
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    return CliRunner().invoke(app, ["init", *arguments], input=input_text)


def test_omitting_all_telegram_flags_skips_the_optional_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    result = _run(["--llm-provider", "openai", "--watchlist", "BTCUSDT"])

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 5]
    assert "Telegram" in result.stdout
    assert "禁用" in result.stdout
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("telegram.chat_ids") is None
    assert backend.values == {}


def test_telegram_flags_store_the_token_write_only_and_complete_step_4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--telegram-token-stdin",
            "--telegram-chat-id",
            "1001",
            "--telegram-user-id",
            "2002",
        ],
        input_text=SENTINEL + "\n",
    )

    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.output
    assert SENTINEL[:12] not in result.output
    assert "****stuv" in result.stdout
    assert backend.values["telegram.bot_token"] == SENTINEL
    assert _progress(settings) == [1, 2, 4, 5]

    service = _service(settings)
    allowlist = service.read_telegram_allowlist()
    assert allowlist.chat_ids == (1001,)
    assert allowlist.allowed_user_ids == (2002,)
    store = SqliteStore(settings)
    store.initialize()
    progress_row = store.get_config("onboarding.progress")
    assert progress_row is not None
    assert SENTINEL not in progress_row.value_json


def test_telegram_flags_apply_after_required_steps_are_complete_and_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    _seed_required_complete(settings)

    result = _run(
        [
            "--resume",
            "--telegram-token-stdin",
            "--telegram-chat-id",
            "1001",
            "--telegram-user-id",
            "2002",
        ],
        input_text=SENTINEL + "\n",
    )

    assert result.exit_code == 0, result.output
    assert "Resuming at step 4/5" in result.stdout
    assert SENTINEL not in result.output
    assert SENTINEL[:12] not in result.output
    assert "****stuv" in result.stdout
    assert backend.values["telegram.bot_token"] == SENTINEL
    assert _progress(settings) == [1, 2, 4, 5]

    service = _service(settings)
    allowlist = service.read_telegram_allowlist()
    assert allowlist.chat_ids == (1001,)
    assert allowlist.allowed_user_ids == (2002,)
    toggles = service.read_telegram_settings().toggles
    assert toggles["scan_report"] is True
    assert toggles["risk"] is True

    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("telegram.chat_ids") is not None
    progress_row = store.get_config(ONBOARDING_PROGRESS_KEY)
    assert progress_row is not None
    assert SENTINEL not in progress_row.value_json


def test_omitted_flags_after_required_steps_skip_step_4_without_completing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    _seed_required_complete(settings)

    result = _run(["--resume"])

    assert result.exit_code == 0, result.output
    assert "禁用" in result.stdout
    assert _progress(settings) == [1, 2, 5]
    assert backend.values == {}
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("telegram.chat_ids") is None


def test_non_interactive_chat_flag_preserves_an_existing_user_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An omitted ``--telegram-user-id`` must keep the stored user allowlist."""
    settings, _ = _configure(monkeypatch, tmp_path)
    container = build_container(settings, initialize=True)
    container.telegram_service.update_settings(
        chat_ids=[1001],
        allowed_user_ids=[2002],
        actor="test",
    )
    _seed_required_complete(settings)
    # A configured environment token satisfies step 4 without stdin.
    monkeypatch.setenv("OFT_TELEGRAM_BOT_TOKEN", SENTINEL)

    result = _run(["--resume", "--telegram-chat-id", "3003"])

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 4, 5]
    service = _service(settings)
    allowlist = service.read_telegram_allowlist()
    assert allowlist.chat_ids == (3003,)
    # The absent user-id flag is "keep existing", never an empty list.
    assert allowlist.allowed_user_ids == (2002,)
    assert "chats 3003" in result.stdout
    assert "users 2002" in result.stdout


def test_interactive_resume_prompts_the_optional_step_after_required_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    _seed_required_complete(settings)
    prompts: list[str] = []

    def _hidden(prompt: str = "") -> str:
        prompts.append(prompt)
        return SENTINEL

    monkeypatch.setattr(init_module.getpass, "getpass", _hidden)

    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert "Resuming at step 4/5" in result.stdout
    assert prompts and "hidden" in prompts[0].lower()
    assert SENTINEL not in result.output
    assert backend.values["telegram.bot_token"] == SENTINEL
    assert _progress(settings) == [1, 2, 4, 5]
    service = _service(settings)
    assert service.read_telegram_allowlist().chat_ids == (1001,)


def test_interactive_reuses_an_environment_token_and_only_persists_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OFT_TELEGRAM_BOT_TOKEN", SENTINEL)
    _seed_required_complete(settings)

    def _unexpected_token_prompt(prompt: str = "") -> str:  # pragma: no cover
        raise AssertionError(f"token must not be re-prompted: {prompt}")

    monkeypatch.setattr(init_module.getpass, "getpass", _unexpected_token_prompt)

    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert _progress(settings) == [1, 2, 4, 5]
    service = _service(settings)
    allowlist = service.read_telegram_allowlist()
    assert allowlist.chat_ids == (1001,)
    assert allowlist.allowed_user_ids == (2002,)


def test_chat_flags_without_a_token_flag_exit_2_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--telegram-chat-id",
            "1001",
        ]
    )

    assert result.exit_code == 2, result.output
    assert "--telegram-token-stdin" in result.output
    assert _progress(settings) == []
    assert backend.values == {}
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("telegram.chat_ids") is None


def test_a_token_without_chat_flags_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--telegram-token-stdin",
        ],
        input_text=SENTINEL + "\n",
    )

    assert result.exit_code == 2, result.output
    assert "--telegram-chat-id" in result.output
    assert SENTINEL not in result.output
    assert _progress(settings) == []


def test_an_invalid_chat_id_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    result = _run(
        [
            "--llm-provider",
            "openai",
            "--watchlist",
            "BTCUSDT",
            "--telegram-token-stdin",
            "--telegram-chat-id",
            "0",
        ],
        input_text=SENTINEL + "\n",
    )

    assert result.exit_code == 2, result.output
    assert _progress(settings) == []
    assert backend.values == {}


def test_non_interactive_token_is_never_a_value_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import inspect

    import typer

    parameters = inspect.signature(init_module.init_command).parameters
    assert "telegram_token_stdin" in parameters
    assert "telegram_token" not in parameters
    assert parameters["telegram_token_stdin"].default is False

    # Width-independent: the rendered help column truncates option names at a
    # narrow terminal (CI), so assert on the Typer/Click option itself. The
    # token must be a boolean stdin switch, never a value-taking option.
    init = typer.main.get_command(app).commands["init"]
    init_params = list(init.params)
    stdin_option = next(
        param for param in init_params if getattr(param, "name", None) == "telegram_token_stdin"
    )
    assert "--telegram-token-stdin" in stdin_option.opts
    assert stdin_option.is_flag is True
    assert stdin_option.secondary_opts == []

    value_token_aliases = [
        param
        for param in init_params
        if not getattr(param, "is_flag", False)
        and (
            "--telegram-token" in getattr(param, "opts", ())
            or "--telegram-token" in getattr(param, "secondary_opts", ())
        )
    ]
    assert value_token_aliases == []


def test_interactive_telegram_step_reads_the_token_hidden_and_can_send_a_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    prompts: list[str] = []

    def _hidden(prompt: str = "") -> str:
        prompts.append(prompt)
        return SENTINEL

    monkeypatch.setattr(init_module.getpass, "getpass", _hidden)

    # Order: step 1 (auto), step 2 (key already configured), step 4 opt-in,
    # hidden token, chat, users, test send, then step 5.
    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\nn\nBTCUSDT\n\n\n",
    )

    assert result.exit_code == 0, result.output
    assert prompts and "hidden" in prompts[0].lower()
    assert SENTINEL not in result.output
    assert "****stuv" in result.stdout
    assert backend.values["telegram.bot_token"] == SENTINEL
    assert _progress(settings) == [1, 2, 4, 5]
    assert "禁用" not in result.stdout
    service = _service(settings)
    assert service.read_telegram_allowlist().chat_ids == (1001,)


def test_interactive_telegram_accepting_the_test_send_queues_one_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    _seed_required_complete(settings)

    def _hidden(prompt: str = "") -> str:
        return SENTINEL

    monkeypatch.setattr(init_module.getpass, "getpass", _hidden)

    # Step 4 only: opt in, token (hidden), chat, users, then accept the test.
    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\ny\n",
    )

    assert result.exit_code == 0, result.output
    # The accepted test is reported by its queued record state and event key.
    assert "test message queued" in result.stdout
    assert "test:1001:" in result.stdout
    # The token is stored write-only and never printed.
    assert SENTINEL not in result.output
    assert SENTINEL[:12] not in result.output
    assert backend.values["telegram.bot_token"] == SENTINEL
    assert _progress(settings) == [1, 2, 4, 5]


def test_interactive_telegram_test_send_failure_never_claims_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _configure(monkeypatch, tmp_path)
    _seed_required_complete(settings)

    def _hidden(prompt: str = "") -> str:
        return SENTINEL

    monkeypatch.setattr(init_module.getpass, "getpass", _hidden)

    def _boom(self: TelegramService, *, actor: str, chat_id: int | None = None) -> Any:
        raise TelegramError("TELEGRAM_TOKEN_INVALID", "the bot token was rejected")

    monkeypatch.setattr(TelegramService, "enqueue_test", _boom)

    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "test message not sent" in result.output
    assert "test message queued" not in result.output
    assert "test message sent" not in result.output
    assert _progress(settings) == [1, 2, 4, 5]


def test_interactive_telegram_skip_writes_nothing_and_states_the_consequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)

    # Blank answer to the opt-in skips the optional step (never completes it).
    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="\nBTCUSDT\n\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "禁用" in result.stdout
    assert _progress(settings) == [1, 2, 5]
    assert backend.values == {}
    store = SqliteStore(settings)
    store.initialize()
    assert store.get_config("telegram.chat_ids") is None


def test_interactive_telegram_invalid_token_exits_2_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, backend = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(init_module.getpass, "getpass", lambda _prompt="": "bad-token")

    result = _interactive(
        monkeypatch,
        ["--resume"],
        input_text="y\n1001\n2002\nn\nBTCUSDT\n\n\n",
    )

    assert result.exit_code == 2, result.output
    assert "bad-token" not in result.output
    assert _progress(settings) == [1, 2]
    assert backend.values == {}


def test_toggles_show_and_set_use_the_shared_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    shown = CliRunner().invoke(app, ["telegram", "toggles", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout[shown.stdout.find("{") :])
    assert payload["data"]["toggles"]["scan_report"] is True
    assert payload["data"]["source"] == "default"

    updated = CliRunner().invoke(
        app,
        [
            "telegram",
            "toggles",
            "set",
            "--no-scan-report",
            "--no-budget",
            "--chat-id",
            "1001",
            "--json",
        ],
    )
    assert updated.exit_code == 0, updated.output
    body = json.loads(updated.stdout[updated.stdout.find("{") :])
    assert body["data"]["toggles"]["scan_report"] is False
    assert body["data"]["toggles"]["budget"] is False
    assert body["data"]["toggles"]["risk"] is True
    assert body["data"]["chat_ids"] == [1001]


def test_toggles_set_refuses_an_enabled_toggle_without_a_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["telegram", "toggles", "set", "--scan-report"])

    assert result.exit_code == 2, result.output
    assert "chat_required_for_pushes" in result.output or "chat" in result.output.lower()
