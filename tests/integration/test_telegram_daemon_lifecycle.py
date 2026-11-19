"""Telegram runtime lifecycle and independence from the other schedulers."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from fakes.telegram import (
    BlockingBotApiTransport,
    MemorySecretBackend,
)
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_bot_api import TelegramBotApiAdapter
from oft.adapters.telegram_fake import TelegramFake
from oft.application.scheduler import scheduler_heartbeat_path
from oft.application.secrets_service import SecretsService
from oft.application.settings_service import SettingsService
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_poller import TelegramPoller, TelegramRuntime
from oft.application.telegram_service import TelegramService
from oft.cli.commands import daemon as daemon_module
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.store import TelegramOutboxRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _components(tmp_path: Path) -> tuple[TelegramRuntime, TelegramPoller, OutboxDispatcher, Any]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    clock = FixedClock(NOW)
    secrets = SecretsService(
        store=store, settings=settings, clock=clock, backend=MemorySecretBackend()
    )
    settings_service = SettingsService(store=store, clock=clock, settings=settings)
    secrets.set_secret("telegram", "bot_token", SENTINEL, actor="test")
    settings_service.update_telegram_allowlist([1001], [2002], actor="test")
    port = TelegramFake()
    service = TelegramService(
        settings=settings,
        store=store,
        clock=clock,
        market_data=FixtureMarketData(fixtures_dir=settings.fixtures_dir),
        secrets=secrets,
        settings_service=settings_service,
        port=port,
    )
    poller = TelegramPoller(
        service=service,
        store=store,
        clock=clock,
        port_resolver=lambda: port,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )
    dispatcher = OutboxDispatcher(store=store, clock=clock, port_resolver=lambda: port)
    runtime = TelegramRuntime(poller=poller, dispatcher=dispatcher, dispatch_interval_seconds=3600)
    return runtime, poller, dispatcher, store


def _row() -> TelegramOutboxRecord:
    import json

    return TelegramOutboxRecord(
        event_key="test:1",
        kind="test",
        chat_id=1001,
        reply_to_message_id=None,
        payload_json=json.dumps({"text": "TELEGRAM TEST"}),
        priority=0,
        state="queued",
        attempts=0,
        next_attempt_at=NOW_TEXT,
        message_id=None,
        last_error_code=None,
        created_at=NOW_TEXT,
        sent_at=None,
    )


def test_the_runtime_starts_and_stops_the_poller_and_dispatcher(tmp_path: Path) -> None:
    runtime, poller, _dispatcher, store = _components(tmp_path)
    store.append_telegram_outbox(_row())

    runtime.start()
    thread = poller._thread  # noqa: SLF001 - the real thread must terminate
    assert thread is not None
    assert poller.is_running is True
    assert runtime.dispatch_running is True
    assert runtime.dispatch_once() == 1

    runtime.shutdown(wait=True)

    assert thread.is_alive() is False
    assert poller.is_running is False
    assert runtime.dispatch_running is False


def test_a_timed_out_join_keeps_the_thread_handle_and_refuses_a_second_poller(
    tmp_path: Path, monkeypatch: Any
) -> None:
    runtime, poller, _dispatcher, _store = _components(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class _BlockingPort(TelegramFake):
        def get_updates(self, *, offset=None, timeout_seconds=0):  # type: ignore[no-untyped-def]
            from oft.ports.telegram import TelegramUpdatePage

            entered.set()
            release.wait(timeout=10)
            return TelegramUpdatePage(())

    poller._port_resolver = _BlockingPort  # noqa: SLF001 - exercise the timeout branch
    monkeypatch.setattr("oft.application.telegram_poller.SHUTDOWN_JOIN_SECONDS", 0.0)

    runtime.start()
    first = poller._thread  # noqa: SLF001
    assert entered.wait(timeout=5)
    try:
        poller.shutdown(wait=True)
        # The join timed out: the handle must remain truthful and no second
        # poller may be spawned alongside the orphaned thread.
        assert poller._thread is first  # noqa: SLF001
        assert poller.is_running is True
        poller.start()
        assert poller._thread is first  # noqa: SLF001 - refused to replace it
    finally:
        release.set()
        first.join(timeout=5)  # type: ignore[union-attr]
        assert first is not None and first.is_alive() is False
        poller.shutdown(wait=True)
    assert poller.is_running is False


def test_the_runtime_close_hook_releases_the_adapter(tmp_path: Path) -> None:
    _runtime, poller, dispatcher, _store = _components(tmp_path)
    closed: list[bool] = []
    runtime = TelegramRuntime(
        poller=poller,
        dispatcher=dispatcher,
        dispatch_interval_seconds=3600,
        closer=lambda: closed.append(True),
    )

    runtime.close()

    assert closed == [True]


def test_a_blocking_telegram_transport_does_not_block_another_scheduler(tmp_path: Path) -> None:
    store = SqliteStore(Settings(env="test", port=8765, data_dir=tmp_path / "data"))
    store.initialize()
    store.append_telegram_outbox(_row())
    transport = BlockingBotApiTransport()
    adapter = TelegramBotApiAdapter(token_resolver=lambda: SENTINEL, transport=transport)
    dispatcher = OutboxDispatcher(store=store, clock=FixedClock(NOW), port_resolver=lambda: adapter)

    dispatch_thread = threading.Thread(target=dispatcher.dispatch_once)
    dispatch_thread.start()
    assert transport.entered.wait(timeout=5)

    ticked = threading.Event()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(lambda: ticked.set(), trigger=IntervalTrigger(seconds=0.05))
    scheduler.start()
    try:
        # The scan/trigger cadence keeps ticking while the Telegram call blocks.
        assert ticked.wait(timeout=5)
    finally:
        transport.release.set()
        dispatch_thread.join(timeout=5)
        scheduler.shutdown(wait=False)


class _StubScheduler:
    def __init__(self, name: str, events: list[str], stopped: bool = True) -> None:
        self._name = name
        self._events = events
        self._stopped = stopped

    def shutdown(self, *, wait: bool = False) -> None:
        self._events.append(f"{self._name}_stop")


class _StubRuntime:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def shutdown(self, *, wait: bool = False) -> None:
        self._events.append("telegram_stop")


def test_daemon_start_starts_every_background_surface_and_serve_stays_api_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """F1: the blessed daemon entrypoint is the only one that starts the runtime."""
    from typer.testing import CliRunner

    from oft.cli.main import app as cli_app

    calls: list[str] = []
    container = type(
        "Container",
        (),
        {"settings": Settings(env="test", port=8765, data_dir=tmp_path / "data")},
    )()

    monkeypatch.setattr(
        "oft.cli.main._resolve_serve_settings", lambda *args, **kwargs: container.settings
    )
    monkeypatch.setattr(
        "oft.cli.main._run_uvicorn", lambda *args, **kwargs: calls.append("uvicorn")
    )
    monkeypatch.setattr("oft.bootstrap.build_container", lambda *args, **kwargs: container)
    monkeypatch.setattr(daemon_module, "_schedule_state", lambda settings: object())

    def _starter(name: str) -> Any:
        def _start(*args: Any, **kwargs: Any) -> _StubScheduler:
            calls.append(name)
            return _StubScheduler(name, calls, stopped=False)

        return _start

    monkeypatch.setattr(
        "oft.cli.main._run_live_ingestion",
        lambda *args, **kwargs: calls.append("ingestion"),
    )
    monkeypatch.setattr(daemon_module, "_start_scheduler", _starter("scan"))
    monkeypatch.setattr(daemon_module, "_start_settings_sync_scheduler", _starter("settings_sync"))
    monkeypatch.setattr(daemon_module, "_start_trigger_scheduler", _starter("trigger"))
    monkeypatch.setattr(daemon_module, "_start_position_monitor_scheduler", _starter("monitor"))
    monkeypatch.setattr(daemon_module, "_start_management_scheduler", _starter("management"))
    monkeypatch.setattr(
        daemon_module,
        "_start_telegram_runtime",
        lambda *args, **kwargs: calls.append("telegram_start"),
    )

    daemon_module.daemon_start()

    for surface in (
        "ingestion",
        "scan",
        "settings_sync",
        "trigger",
        "monitor",
        "management",
        "telegram_start",
        "uvicorn",
    ):
        assert surface in calls, surface

    calls.clear()
    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls == ["uvicorn"]


def test_daemon_start_stops_the_live_ingestion_handle_before_schedulers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    events: list[str] = []

    class _Handle:
        def stop(self, *, timeout: float | None = None) -> bool:
            events.append("ingestion_stop")
            return True

    container = type(
        "Container",
        (),
        {"settings": Settings(env="test", port=8765, data_dir=tmp_path / "data")},
    )()
    monkeypatch.setattr(
        "oft.cli.main._resolve_serve_settings", lambda *args, **kwargs: container.settings
    )
    monkeypatch.setattr("oft.cli.main._run_live_ingestion", lambda *args, **kwargs: _Handle())
    monkeypatch.setattr("oft.cli.main._run_uvicorn", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "_schedule_state", lambda settings: object())
    monkeypatch.setattr("oft.bootstrap.build_container", lambda *args, **kwargs: container)

    def _starter(name: str) -> Any:
        def _start(*args: Any, **kwargs: Any) -> _StubScheduler:
            events.append(f"{name}_start")
            return _StubScheduler(name, events)

        return _start

    monkeypatch.setattr(daemon_module, "_start_scheduler", _starter("scan"))
    monkeypatch.setattr(daemon_module, "_start_settings_sync_scheduler", _starter("settings_sync"))
    monkeypatch.setattr(daemon_module, "_start_trigger_scheduler", _starter("trigger"))
    monkeypatch.setattr(daemon_module, "_start_position_monitor_scheduler", _starter("monitor"))
    monkeypatch.setattr(daemon_module, "_start_management_scheduler", _starter("management"))
    monkeypatch.setattr(
        daemon_module,
        "_start_telegram_runtime",
        lambda *args, **kwargs: _StubRuntime(events),
    )

    daemon_module.daemon_start()

    assert events.index("ingestion_stop") < events.index("telegram_stop")
    assert events.index("ingestion_stop") < events.index("management_stop")
    assert events.index("ingestion_stop") < events.index("scan_stop")


def test_daemon_start_starts_and_stops_telegram_first(tmp_path: Path, monkeypatch: Any) -> None:
    events: list[str] = []
    container = type(
        "Container",
        (),
        {"settings": Settings(env="test", port=8765, data_dir=tmp_path / "data")},
    )()

    monkeypatch.setattr(
        "oft.cli.main._resolve_serve_settings", lambda *args, **kwargs: container.settings
    )
    monkeypatch.setattr("oft.cli.main._run_live_ingestion", lambda *args, **kwargs: None)
    monkeypatch.setattr("oft.cli.main._run_uvicorn", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "_schedule_state", lambda settings: object())
    monkeypatch.setattr("oft.bootstrap.build_container", lambda *args, **kwargs: container)

    def _starter(name: str, *, stopped: bool = True) -> Any:
        def _start(*args: Any, **kwargs: Any) -> _StubScheduler:
            events.append(name)
            return _StubScheduler(name, events, stopped)

        return _start

    monkeypatch.setattr(daemon_module, "_start_scheduler", _starter("scan"))
    monkeypatch.setattr(
        daemon_module, "_start_settings_sync_scheduler", _starter("settings_sync", stopped=False)
    )
    monkeypatch.setattr(daemon_module, "_start_trigger_scheduler", _starter("trigger"))
    monkeypatch.setattr(daemon_module, "_start_position_monitor_scheduler", _starter("monitor"))
    monkeypatch.setattr(daemon_module, "_start_management_scheduler", _starter("management"))

    def _telegram(*args: Any, **kwargs: Any) -> _StubRuntime:
        events.append("telegram_start")
        runtime = _StubRuntime(events)
        return runtime

    monkeypatch.setattr(daemon_module, "_start_telegram_runtime", _telegram)

    daemon_module.daemon_start()

    assert events.index("telegram_start") > events.index("management")
    telegram_stop = events.index("telegram_stop")
    assert events[telegram_stop + 1] == "management_stop"
    assert events[-1] == "scan_stop"


def _patch_daemon_surfaces(
    monkeypatch: Any,
    settings: Settings,
    container: Any,
    events: list[str],
    *,
    trigger_start: Any = None,
) -> None:
    monkeypatch.setattr("oft.cli.main._resolve_serve_settings", lambda *a, **k: settings)
    monkeypatch.setattr("oft.cli.main._run_uvicorn", lambda *a, **k: None)
    monkeypatch.setattr("oft.bootstrap.build_container", lambda *a, **k: container)
    monkeypatch.setattr(daemon_module, "_schedule_state", lambda _settings: object())

    def _starter(name: str) -> Any:
        def _start(*args: Any, **kwargs: Any) -> _StubScheduler:
            events.append(f"{name}_start")
            return _StubScheduler(name, events)

        return _start

    monkeypatch.setattr(daemon_module, "_start_scheduler", _starter("scan"))
    monkeypatch.setattr(daemon_module, "_start_settings_sync_scheduler", _starter("settings_sync"))
    monkeypatch.setattr(
        daemon_module,
        "_start_trigger_scheduler",
        trigger_start or _starter("trigger"),
    )
    monkeypatch.setattr(daemon_module, "_start_position_monitor_scheduler", _starter("monitor"))
    monkeypatch.setattr(daemon_module, "_start_management_scheduler", _starter("management"))
    monkeypatch.setattr(daemon_module, "_start_telegram_runtime", lambda *a, **k: None)


def test_daemon_start_removes_the_scheduler_heartbeat_on_shutdown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    events: list[str] = []
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    heartbeat = scheduler_heartbeat_path(settings)
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text('{"updated_at": "2026-11-17T09:30:00.000Z"}', encoding="utf-8")
    container = type("Container", (), {"settings": settings})()
    _patch_daemon_surfaces(monkeypatch, settings, container, events)
    monkeypatch.setattr("oft.cli.main._run_live_ingestion", lambda *a, **k: None)

    daemon_module.daemon_start()

    assert not heartbeat.exists()


def test_daemon_start_stops_ingestion_and_started_schedulers_when_a_starter_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    events: list[str] = []
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = type("Container", (), {"settings": settings})()

    class _Handle:
        def stop(self, *, timeout: float | None = None) -> bool:
            events.append("ingestion_stop")
            return True

    def _fail(*args: Any, **kwargs: Any) -> Any:
        events.append("trigger_start")
        raise RuntimeError("scheduler boom")

    _patch_daemon_surfaces(monkeypatch, settings, container, events, trigger_start=_fail)
    monkeypatch.setattr("oft.cli.main._run_live_ingestion", lambda *a, **k: _Handle())

    with pytest.raises(RuntimeError, match="scheduler boom"):
        daemon_module.daemon_start()

    # The live worker and every scheduler that started before the failure were
    # stopped; the failing trigger scheduler never returned to be shut down.
    assert "ingestion_stop" in events
    assert "scan_stop" in events
    assert "settings_sync_stop" in events
    assert "trigger_stop" not in events
