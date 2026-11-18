"""``oft telegram status|test|receipts|token|allowlist`` CLI surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fakes.telegram import MemorySecretBackend
from oft.application import secrets_service as secrets_module
from oft.application.health import DaemonProbe
from oft.cli.main import app

SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[MemorySecretBackend, Path]:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_PORT", "8765")
    monkeypatch.setenv("OFT_DATA", str(data_dir))
    return memory, data_dir


def _invoke(*args: str, input_text: str | None = None) -> Any:
    return CliRunner().invoke(app, list(args), input=input_text)


def _payload(output: str) -> dict[str, Any]:
    """Parse the envelope even when a structlog line precedes it on stdout."""
    start = output.find("{\n")
    return json.loads(output if start == -1 else output[start:])


def test_status_reports_not_configured(configured: Any) -> None:
    result = _invoke("telegram", "status", "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result.stdout)
    assert payload["data"]["state"] == "NOT_CONFIGURED"


def test_token_set_reads_stdin_and_prints_only_the_mask(configured: Any) -> None:
    result = _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n")

    assert result.exit_code == 0, result.output
    assert SENTINEL not in result.stdout
    assert "****stuv" in result.stdout


def test_token_set_requires_stdin(configured: Any) -> None:
    result = _invoke("telegram", "token", "set")

    assert result.exit_code == 2
    assert "stdin" in result.output


def test_allowlist_add_show_and_remove(configured: Any) -> None:
    added = _invoke(
        "telegram",
        "allowlist",
        "add",
        "--chat-id",
        "1001",
        "--user-id",
        "2002",
        "--json",
    )
    assert added.exit_code == 0, added.output
    assert _payload(added.stdout)["data"]["chat_ids"] == [1001]

    shown = _invoke("telegram", "allowlist", "show", "--json")
    assert _payload(shown.stdout)["data"]["allowed_user_ids"] == [2002]

    removed = _invoke("telegram", "allowlist", "remove", "--chat-id", "1001", "--json")
    assert removed.exit_code == 0, removed.output
    assert _payload(removed.stdout)["data"]["chat_ids"] == []


def test_allowlist_requires_at_least_one_selector(configured: Any) -> None:
    result = _invoke("telegram", "allowlist", "add", "--json")

    assert result.exit_code == 2
    assert "chat-id" in result.output or "user-id" in result.output


def test_test_send_and_receipts_after_configuration(configured: Any) -> None:
    token = _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n")
    assert token.exit_code == 0, token.output
    allow = _invoke("telegram", "allowlist", "add", "--chat-id", "1001", "--json")
    assert allow.exit_code == 0, allow.output

    sent = _invoke("telegram", "test", "--json")
    assert sent.exit_code == 0, sent.output
    assert _payload(sent.stdout)["data"]["kind"] == "test"

    receipts = _invoke("telegram", "receipts", "--limit", "5", "--json")
    assert receipts.exit_code == 0, receipts.output
    assert _payload(receipts.stdout)["data"]["total"] == 1
    assert SENTINEL not in receipts.stdout


def test_token_delete_removes_the_stored_value(configured: Any) -> None:
    assert _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n").exit_code == 0

    deleted = _invoke("telegram", "token", "delete", "--json")

    assert deleted.exit_code == 0, deleted.output
    assert _payload(deleted.stdout)["data"]["configured"] is False
    assert SENTINEL not in deleted.stdout


def test_status_human_output_is_readable(configured: Any) -> None:
    result = _invoke("telegram", "status")

    assert result.exit_code == 0, result.output
    assert "state" in result.stdout and "NOT_CONFIGURED" in result.stdout
    assert "chats" in result.stdout


def test_an_invalid_token_is_a_validation_error(configured: Any) -> None:
    result = _invoke("telegram", "token", "set", "--stdin", input_text="not-a-token\n")

    assert result.exit_code == 2
    assert "bot_token" in result.output


def test_watchdog_requires_the_one_shot_flag(configured: Any) -> None:
    result = _invoke("telegram", "watchdog")

    assert result.exit_code == 2
    assert "--once" in result.output


def test_watchdog_once_never_prints_the_token(configured: Any, monkeypatch: Any) -> None:
    token = _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n")
    assert token.exit_code == 0, token.output
    monkeypatch.setattr(
        "oft.cli.commands.telegram.probe_daemon",
        lambda settings, timeout=0.25: DaemonProbe(state="OFFLINE", detail="no listener"),
    )

    result = _invoke("telegram", "watchdog", "--once", "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result.stdout)
    assert payload["data"]["daemon_state"] == "OFFLINE"
    # No allowlisted private chat is configured, so no send happens.
    assert payload["data"]["action"] == "NONE"
    assert SENTINEL not in result.stdout


def _configure_private_watchdog(monkeypatch: Any) -> None:
    assert _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n").exit_code == 0
    assert _invoke("telegram", "allowlist", "add", "--chat-id", "1001").exit_code == 0
    monkeypatch.setattr(
        "oft.cli.commands.telegram.probe_daemon",
        lambda settings, timeout=0.25: DaemonProbe(state="OFFLINE", detail="no listener"),
    )


def _record_sends(monkeypatch: Any) -> list[dict[str, Any]]:
    from oft.adapters.telegram_fake import TelegramFake

    sent: list[dict[str, Any]] = []
    real_send = TelegramFake.send_message

    def _recording(self: Any, **kwargs: Any) -> Any:
        sent.append(dict(kwargs))
        return real_send(self, **kwargs)

    monkeypatch.setattr(TelegramFake, "send_message", _recording)
    return sent


def test_watchdog_alerts_once_on_an_allowlisted_private_chat(
    configured: Any, monkeypatch: Any
) -> None:
    _configure_private_watchdog(monkeypatch)
    sent = _record_sends(monkeypatch)

    first = _invoke("telegram", "watchdog", "--once", "--json")
    second = _invoke("telegram", "watchdog", "--once", "--json")

    assert first.exit_code == 0, first.output
    assert _payload(first.stdout)["data"]["action"] == "ALERT_SENT"
    assert _payload(first.stdout)["data"]["daemon_state"] == "OFFLINE"
    assert second.exit_code == 0, second.output
    assert _payload(second.stdout)["data"]["action"] == "NONE"
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 1001
    assert SENTINEL not in sent[0]["text"]
    assert SENTINEL not in first.stdout


def test_watchdog_group_only_allowlist_is_a_quiet_noop(configured: Any, monkeypatch: Any) -> None:
    assert _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n").exit_code == 0
    assert _invoke("telegram", "allowlist", "add", "--chat-id=-100200").exit_code == 0
    monkeypatch.setattr(
        "oft.cli.commands.telegram.probe_daemon",
        lambda settings, timeout=0.25: DaemonProbe(state="OFFLINE", detail="no listener"),
    )
    sent = _record_sends(monkeypatch)

    result = _invoke("telegram", "watchdog", "--once", "--json")

    assert result.exit_code == 0, result.output
    assert _payload(result.stdout)["data"]["action"] == "NONE"
    assert sent == []
    assert SENTINEL not in result.stdout


def test_watchdog_reports_a_failed_send_as_a_nonzero_exit(
    configured: Any, monkeypatch: Any
) -> None:
    from oft.adapters.telegram_fake import TelegramFake
    from oft.ports.telegram import TELEGRAM_UPSTREAM_ERROR, TelegramError

    _configure_private_watchdog(monkeypatch)

    def _fail(self: Any, *, chat_id: int, text: str, reply_to_message_id: Any = None) -> Any:
        raise TelegramError(TELEGRAM_UPSTREAM_ERROR, "scripted send failure")

    monkeypatch.setattr(TelegramFake, "send_message", _fail)

    text_result = _invoke("telegram", "watchdog", "--once")
    assert text_result.exit_code == 1, text_result.output
    assert "ALERT_FAILED" in text_result.output
    assert "error:" in text_result.output
    assert SENTINEL not in text_result.output

    json_result = _invoke("telegram", "watchdog", "--once", "--json")
    assert json_result.exit_code == 1, json_result.output
    assert _payload(json_result.stdout)["error"]["code"] == "TELEGRAM_DELIVERY_FAILED"
    assert SENTINEL not in json_result.stdout


def test_watchdog_reports_an_unreadable_state_as_a_nonzero_exit(
    configured: Any, monkeypatch: Any
) -> None:
    from oft.adapters.sqlite_store import SqliteStore

    real_get = SqliteStore.get_system_state

    def _raise_for_watchdog(self: Any, key: str) -> Any:
        if key == "telegram.watchdog_state":
            raise RuntimeError("state unavailable")
        return real_get(self, key)

    monkeypatch.setattr(SqliteStore, "get_system_state", _raise_for_watchdog)

    text_result = _invoke("telegram", "watchdog", "--once")
    assert text_result.exit_code == 1, text_result.output
    assert "UNKNOWN" in text_result.output
    assert "error:" in text_result.output

    json_result = _invoke("telegram", "watchdog", "--once", "--json")
    assert json_result.exit_code == 1, json_result.output
    assert _payload(json_result.stdout)["error"]["code"] == "DATA_UNAVAILABLE"


def test_watchdog_reports_an_unavailable_lock_as_a_nonzero_exit(
    configured: Any, monkeypatch: Any
) -> None:
    _memory, data_dir = configured
    _configure_private_watchdog(monkeypatch)
    sent = _record_sends(monkeypatch)
    # A directory at the lock path makes ``open(path, "a+")`` raise an OSError.
    (data_dir / "telegram-watchdog.lock").mkdir(parents=True, exist_ok=True)

    result = _invoke("telegram", "watchdog", "--once", "--json")

    assert result.exit_code == 1, result.output
    assert _payload(result.stdout)["error"]["code"] == "DATA_UNAVAILABLE"
    assert "Traceback" not in result.output
    assert sent == []


def test_test_send_without_a_token_reports_not_configured(configured: Any) -> None:
    result = _invoke("telegram", "test", "--json")

    assert result.exit_code == 1
    assert _payload(result.stdout)["error"]["code"] == "TELEGRAM_NOT_CONFIGURED"


def test_an_invalid_token_is_mapped_at_the_test_send_boundary(
    configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.adapters.telegram_fake import TelegramFake
    from oft.ports.telegram import TELEGRAM_TOKEN_INVALID, TelegramError

    def _unauthorized(self: Any) -> Any:
        raise TelegramError(TELEGRAM_TOKEN_INVALID, "scripted 401")

    monkeypatch.setattr(TelegramFake, "get_me", _unauthorized)
    assert _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n").exit_code == 0
    assert _invoke("telegram", "allowlist", "add", "--chat-id", "1001").exit_code == 0

    result = _invoke("telegram", "test", "--json")

    assert result.exit_code == 1
    assert _payload(result.stdout)["error"]["code"] == "TELEGRAM_TOKEN_INVALID"
    assert SENTINEL not in result.stdout


def test_receipts_and_allowlist_human_output(configured: Any) -> None:
    assert _invoke("telegram", "token", "set", "--stdin", input_text=SENTINEL + "\n").exit_code == 0
    assert (
        _invoke("telegram", "allowlist", "add", "--chat-id", "1001", "--user-id", "2002").exit_code
        == 0
    )
    assert _invoke("telegram", "test").exit_code == 0

    receipts = _invoke("telegram", "receipts")
    show = _invoke("telegram", "allowlist", "show")
    deleted = _invoke("telegram", "token", "delete")

    assert receipts.exit_code == 0
    assert "EVENT" in receipts.stdout and "test:" in receipts.stdout
    assert "chat_ids" in show.stdout and "1001" in show.stdout
    assert deleted.exit_code == 0 and "deleted" in deleted.stdout

    empty = _invoke("telegram", "receipts")
    assert empty.exit_code == 0


def test_a_corrupt_persisted_allowlist_is_a_config_invalid_envelope(configured: Any) -> None:
    from oft.adapters.sqlite_store import SqliteStore
    from oft.config import Settings
    from oft.domain.telegram import TELEGRAM_CHAT_IDS_KEY
    from oft.ports.store import ConfigRecord

    _memory, data_dir = configured
    settings = Settings(env="test", port=8765, data_dir=data_dir)
    store = SqliteStore(settings)
    store.initialize()
    store.set_config(
        record=ConfigRecord(
            key=TELEGRAM_CHAT_IDS_KEY,
            value_json="not-json",
            updated_at="2026-11-17T09:30:00.000Z",
            updated_by="test",
        )
    )

    result = _invoke("telegram", "status", "--json")

    assert result.exit_code == 5, result.output
    assert _payload(result.stdout)["error"]["code"] == "CONFIG_INVALID"
    assert "Traceback" not in result.output
