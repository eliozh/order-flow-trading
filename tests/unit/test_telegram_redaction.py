"""Telegram-token redaction in logs and the secret lifecycle."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import structlog

from oft import bootstrap
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.application.secrets_service import SecretsService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import validate_telegram_token
from oft.logging import (
    REDACTED,
    RedactingFilter,
    _scrub_text,
    configure_logging,
    redact_secrets,
    set_redaction_values,
)

SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
OTHER = "987654321:BBOtherTelegramToken_abcdefghijklmnopqrstuv"

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return store


class MemoryBackend:
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


def test_a_token_embedded_in_free_text_is_scrubbed() -> None:
    event = {
        "event": "telegram_poll_failed",
        "error": f"request failed with token {SENTINEL}",
        "message": f"prefix {SENTINEL} suffix",
        "detail": "unrelated",
    }

    redacted = redact_secrets(None, "error", event)

    assert SENTINEL not in json.dumps(redacted)
    assert REDACTED in redacted["error"]
    assert REDACTED in redacted["message"]
    assert redacted["detail"] == "unrelated"


def test_a_secret_named_key_is_still_fully_redacted() -> None:
    redacted = redact_secrets(None, "info", {"token": SENTINEL, "bot_token": SENTINEL})

    assert redacted["token"] == REDACTED
    assert redacted["bot_token"] == REDACTED


def test_bearer_tokens_still_scrub() -> None:
    redacted = redact_secrets(None, "info", {"error": "Authorization: Bearer abc.def"})

    assert "abc.def" not in redacted["error"]
    assert REDACTED in redacted["error"]


def test_redaction_values_include_the_stored_and_environment_token(tmp_path: Path) -> None:
    backend = MemoryBackend()
    store = _store(tmp_path)
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        telegram_bot_token=OTHER,
    )
    service = SecretsService(store=store, settings=settings, clock=FixedClock(NOW), backend=backend)
    service.set_secret("telegram", "bot_token", SENTINEL, actor="test")

    assert set(service.redaction_values()) >= {SENTINEL, OTHER}


def test_the_token_status_never_contains_the_value(tmp_path: Path) -> None:
    backend = MemoryBackend()
    store = _store(tmp_path)
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    service = SecretsService(store=store, settings=settings, clock=FixedClock(NOW), backend=backend)

    status = service.set_secret("telegram", "bot_token", SENTINEL, actor="web")
    payload = json.dumps(status.to_contract())

    assert status.configured is True
    assert status.masked == "****stuv"
    assert status.last4 == "stuv"
    assert status.key_source == "encrypted_file"
    assert SENTINEL not in payload
    assert "value" not in payload


def test_a_stored_token_wins_over_the_environment(tmp_path: Path) -> None:
    backend = MemoryBackend()
    store = _store(tmp_path)
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        telegram_bot_token=OTHER,
    )
    service = SecretsService(store=store, settings=settings, clock=FixedClock(NOW), backend=backend)

    assert service.resolve_secret("telegram", "bot_token") == OTHER
    service.set_secret("telegram", "bot_token", SENTINEL, actor="web")
    assert service.resolve_secret("telegram", "bot_token") == SENTINEL

    service.delete_secret("telegram", "bot_token", actor="web")
    assert service.resolve_secret("telegram", "bot_token") == OTHER


def test_the_secret_metadata_row_carries_only_non_secret_fields(tmp_path: Path) -> None:
    backend = MemoryBackend()
    store = _store(tmp_path)
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    service = SecretsService(store=store, settings=settings, clock=FixedClock(NOW), backend=backend)

    validate_telegram_token(SENTINEL)
    service.set_secret("telegram", "bot_token", SENTINEL, actor="web")

    meta = store.get_secret_meta("telegram", "bot_token")
    assert meta is not None
    row_text = json.dumps(meta.__dict__)
    assert SENTINEL not in row_text
    assert meta.provider == "telegram"
    assert meta.name == "bot_token"
    assert meta.last4 == "stuv"
    assert meta.backend == backend.kind


def test_the_httpx_request_url_never_reaches_stdout_or_the_daemon_log(
    tmp_path: Path, caplog: object
) -> None:
    """The adapter puts the token in the URL path; httpx logs that URL at INFO.

    This drives the real adapter over a ``MockTransport`` (no network), then
    emits the exact stdlib request line ``httpx``/``httpcore`` produce. Both
    root handlers must scrub it, and the raw value must be absent from
    ``caplog.text`` and from the on-disk ``daemon.log``.
    """
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        log_level="INFO",
    )
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    try:
        configure_logging(settings)
        set_redaction_values((SENTINEL,))
        # ``basicConfig(force=True)`` replaces the root handlers; re-attach the
        # caplog handler so the assertion covers the captured stream too.
        root.addHandler(caplog.handler)  # type: ignore[attr-defined]
        caplog.set_level(logging.INFO)  # type: ignore[attr-defined]

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": True, "result": {"id": 42, "username": "oft_bot"}}
            )
        )
        adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)
        try:
            identity = adapter.get_me()
        finally:
            adapter.close()

        # The production leak path: httpx emits this at INFO, with the token in
        # the URL path. Emit it explicitly because the logger is silenced.
        logging.getLogger("oft.test").info(
            "HTTP Request: POST https://api.telegram.org/bot%s/getMe '200 OK'", SENTINEL
        )
        httpx_logger.warning(
            "HTTP Request: POST https://api.telegram.org/bot%s/getMe '200 OK'", SENTINEL
        )

        assert identity.user_id == 42
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
        assert SENTINEL not in caplog.text  # type: ignore[attr-defined]
        daemon_log = (settings.data_dir / "logs" / "daemon.log").read_text(encoding="utf-8")
        assert "HTTP Request" in daemon_log
        assert SENTINEL not in daemon_log
    finally:
        root.handlers[:] = original_handlers
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)
        set_redaction_values(())


def test_an_unshaped_registered_value_is_still_scrubbed() -> None:
    value = "arbitrary-env-token-without-the-bot-api-shape"
    set_redaction_values((value,))
    try:
        redacted = redact_secrets(None, "info", {"error": f"failed with {value}"})
        assert value not in json.dumps(redacted)
        assert REDACTED in redacted["error"]
    finally:
        set_redaction_values(())


def test_the_token_shape_scrubs_the_bot_url_with_an_empty_registry() -> None:
    """The real leak form is ``/bot<token>/getMe``, not a free-standing token."""
    set_redaction_values(())
    try:
        scrubbed = _scrub_text(f"https://api.telegram.org/bot{SENTINEL}/getMe")
    finally:
        set_redaction_values(())

    assert SENTINEL not in scrubbed
    assert REDACTED in scrubbed


def test_the_stdlib_filter_scrubs_the_bot_url_with_an_empty_registry() -> None:
    set_redaction_values(())
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: POST https://api.telegram.org/bot%s/getMe '200 OK'",
        args=(SENTINEL,),
        exc_info=None,
    )
    try:
        assert RedactingFilter().filter(record) is True
        rendered = record.getMessage()
    finally:
        set_redaction_values(())

    assert SENTINEL not in rendered
    assert REDACTED in rendered


def test_a_token_ending_in_a_dash_is_valid_and_redacted() -> None:
    token = "123456789:" + "A" * 30 + "-"

    assert validate_telegram_token(token) == token
    set_redaction_values(())
    try:
        scrubbed = _scrub_text(f"https://api.telegram.org/bot{token}/getMe")
    finally:
        set_redaction_values(())

    assert token not in scrubbed
    assert REDACTED in scrubbed


def test_build_container_seeds_the_log_redaction_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the ``set_redaction_values`` wiring in bootstrap must fail here."""
    wired_value = "plain-env-telegram-token-value"
    captured: list[tuple[str, ...]] = []

    class _FakeSecretsService:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def redaction_values(self, *, include_providers: bool = True) -> tuple[str, ...]:
            assert include_providers is False
            return (wired_value,)

        def resolve_secret(self, _subject: str, _name: str) -> str | None:
            return None

    monkeypatch.setattr(bootstrap, "SecretsService", _FakeSecretsService)
    monkeypatch.setattr(
        bootstrap,
        "resolve_effective_llm_settings",
        lambda *, settings, store, clock: settings,
    )

    def _capture(values: tuple[str, ...]) -> None:
        captured.append(tuple(values))
        set_redaction_values(values)

    monkeypatch.setattr(bootstrap, "set_redaction_values", _capture)
    try:
        bootstrap.build_container(Settings(env="test", port=8765, data_dir=tmp_path / "data"))

        assert captured, "build_container did not seed the redaction registry"
        assert wired_value in captured[0]

        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="HTTP Request: POST https://api.telegram.org/bot%s/getMe '200 OK'",
            args=(wired_value,),
            exc_info=None,
        )
        assert RedactingFilter().filter(record) is True
        assert wired_value not in record.getMessage()
    finally:
        set_redaction_values(())


def test_a_stdlib_exception_traceback_is_scrubbed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``logger.exception`` whose exception text carries the token is scrubbed.

    The stdlib formatter appends ``exc_text`` after the handler filter runs, so
    only a scrubbing formatter (or a filter-computed ``exc_text``) closes this
    leak. Both root handlers must emit the redacted traceback.
    """
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        log_level="INFO",
    )
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        configure_logging(settings)
        set_redaction_values(())
        logger = logging.getLogger("oft.test")
        try:
            raise RuntimeError(f"cannot reach https://api.telegram.org/bot{SENTINEL}/getMe")
        except RuntimeError:
            logger.exception("direct %s", SENTINEL)
        captured = capsys.readouterr()
        daemon_log = (settings.data_dir / "logs" / "daemon.log").read_text(encoding="utf-8")
    finally:
        root.handlers[:] = original_handlers
        set_redaction_values(())

    assert "direct [REDACTED]" in daemon_log
    assert "RuntimeError" in daemon_log
    assert SENTINEL not in daemon_log
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_the_filter_scrubs_a_rendered_traceback() -> None:
    set_redaction_values(())
    try:
        try:
            raise RuntimeError(f"cannot reach https://api.telegram.org/bot{SENTINEL}/getMe")
        except RuntimeError:
            record = logging.LogRecord(
                name="oft.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        assert RedactingFilter().filter(record) is True
        rendered = logging.Formatter("%(message)s").format(record)
    finally:
        set_redaction_values(())

    assert "RuntimeError" in rendered
    assert SENTINEL not in rendered


def test_a_structlog_exception_is_scrubbed_in_the_daemon_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The processor chain must format ``exc_info`` before redaction runs."""
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        log_level="INFO",
    )
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        configure_logging(settings)
        set_redaction_values(())
        logger = structlog.get_logger("oft.structlog.test")
        try:
            raise RuntimeError(f"cannot reach https://api.telegram.org/bot{SENTINEL}/getMe")
        except RuntimeError:
            logger.exception("structlog failure")
        captured = capsys.readouterr()
        daemon_log = (settings.data_dir / "logs" / "daemon.log").read_text(encoding="utf-8")
    finally:
        root.handlers[:] = original_handlers
        set_redaction_values(())
        structlog.reset_defaults()

    assert "structlog failure" in daemon_log
    assert SENTINEL not in daemon_log
    assert SENTINEL not in captured.out
