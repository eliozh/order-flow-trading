"""Poller: authorization, command dispatch, offset persistence, and replay safety."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fakes.telegram import MemorySecretBackend, make_update
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.sqlite_store import SqliteStore
from oft.adapters.telegram_fake import TelegramFake
from oft.application.secrets_service import SecretsService
from oft.application.settings_service import SettingsService
from oft.application.status import build_status
from oft.application.telegram_messages import render_status
from oft.application.telegram_poller import TelegramPoller
from oft.application.telegram_service import TelegramService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.telegram import (
    POLLER_HEARTBEAT_KEY,
    REFUSAL_TEXT,
    TELEGRAM_CHAT_IDS_KEY,
    parse_update,
)
from oft.ports.store import ConfigRecord

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"
SENTINEL_TOKEN = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


def _service(tmp_path: Path) -> tuple[TelegramService, SqliteStore, TelegramFake, Settings]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    clock = FixedClock(NOW)
    backend = MemorySecretBackend()
    secrets = SecretsService(store=store, settings=settings, clock=clock, backend=backend)
    settings_service = SettingsService(store=store, clock=clock, settings=settings)
    secrets.set_secret("telegram", "bot_token", SENTINEL_TOKEN, actor="test")
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
    return service, store, port, settings


def _poller(service: TelegramService, store: SqliteStore, port: TelegramFake) -> TelegramPoller:
    return TelegramPoller(
        service=service,
        store=store,
        clock=FixedClock(NOW),
        port_resolver=lambda: port,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )


def test_allowlisted_status_enqueues_exactly_one_matching_reply(tmp_path: Path) -> None:
    service, store, port, settings = _service(tmp_path)
    update = make_update(1, chat_id=1001, user_id=2002, text="/status")
    port.script_updates([update])
    poller = _poller(service, store, port)

    processed = poller.poll_once()

    assert processed == 1
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.kind == "query_reply"
    assert receipt.chat_id == 1001
    assert receipt.reply_to_message_id == 101
    assert receipt.state == "queued"
    expected = render_status(
        build_status(
            settings,
            FixtureMarketData(fixtures_dir=settings.fixtures_dir),
            FixedClock(NOW),
            store=store,
        )
    )
    assert json.loads(receipt.payload_json)["text"] == expected
    assert store.get_system_state("telegram.poll_offset") == "2"


def test_a_second_poll_does_not_replay_the_same_update(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1)])

    poller = _poller(service, store, port)
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0
    assert len(store.list_telegram_receipts()) == 1


def test_a_simulated_restart_does_not_replay(tmp_path: Path) -> None:
    service, store, port, settings = _service(tmp_path)
    port.script_updates([make_update(1)])
    assert _poller(service, store, port).poll_once() == 1

    # A brand-new poller over the same store reads the persisted offset.
    restarted = _poller(service, store, port)
    assert restarted.poll_once() == 0
    assert len(store.list_telegram_receipts()) == 1


def test_a_crash_between_enqueue_and_offset_persist_cannot_duplicate(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    update = make_update(1)
    # The service enqueued the reply, then the process died before the offset
    # was advanced: the replay must be a no-op via the unique event_key.
    service.handle_update(parse_update(update))
    port.script_updates([update])

    assert _poller(service, store, port).poll_once() == 1
    assert len(store.list_telegram_receipts()) == 1
    assert store.get_system_state("telegram.poll_offset") == "2"


def test_an_unknown_chat_gets_the_fixed_refusal_and_no_status_data(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, chat_id=9999, user_id=2002, text="/status")])

    assert _poller(service, store, port).poll_once() == 1
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "refusal"
    assert json.loads(receipts[0].payload_json)["text"] == REFUSAL_TEXT
    assert "version:" not in receipts[0].payload_json
    assert "symbols:" not in receipts[0].payload_json
    audits = [event.event_type for event in store.list_audit_events_ascending()]
    assert "TELEGRAM_UNAUTHORIZED" in audits


def test_an_allowed_chat_with_a_non_allowlisted_user_is_refused_exactly(
    tmp_path: Path,
) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, chat_id=1001, user_id=9999, text="/status")])

    assert _poller(service, store, port).poll_once() == 1

    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "refusal"
    assert json.loads(receipts[0].payload_json)["text"] == REFUSAL_TEXT
    assert "version:" not in receipts[0].payload_json
    audits = [event.event_type for event in store.list_audit_events_ascending()]
    assert audits.count("TELEGRAM_UNAUTHORIZED") == 1


def test_repeated_refusals_are_rate_limited_and_dropped_silently(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(index, chat_id=9999, user_id=2002) for index in range(1, 5)])

    assert _poller(service, store, port).poll_once() == 4

    assert len(store.list_telegram_receipts()) == 3
    audits = [event.event_type for event in store.list_audit_events_ascending()]
    assert audits.count("TELEGRAM_UNAUTHORIZED") == 3


def test_a_status_embedded_in_prose_is_never_executed(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, text="please run /status now")])

    assert _poller(service, store, port).poll_once() == 1
    assert store.list_telegram_receipts() == ()


def test_an_unknown_command_is_ignored(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, text="/approve")])

    assert _poller(service, store, port).poll_once() == 1
    assert store.list_telegram_receipts() == ()


def test_a_control_character_obfuscated_status_is_sanitized_and_runs(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, text="/sta\x00tus")])

    assert _poller(service, store, port).poll_once() == 1
    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "query_reply"


def test_help_reply_lists_only_the_supported_commands(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, text="/help")])

    assert _poller(service, store, port).poll_once() == 1
    text = store.list_telegram_receipts()[0].payload_json
    assert "/status" in text and "/help" in text
    assert "/approve" not in text


def test_the_poller_thread_starts_and_stops_cleanly(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    poller = _poller(service, store, port)

    poller.start()
    assert poller.is_running is True
    poller.shutdown(wait=True)

    assert poller.is_running is False


def test_an_old_update_returned_by_the_backend_is_skipped(tmp_path: Path) -> None:
    class _IgnoringOffset(TelegramFake):
        def get_updates(self, *, offset=None, timeout_seconds=0):  # type: ignore[no-untyped-def]
            self.update_calls.append(offset)
            return tuple(self._updates)

    service, store, _port, _settings = _service(tmp_path)
    port = _IgnoringOffset()
    port.script_updates([make_update(1)])
    store.set_system_state("telegram.poll_offset", "2", updated_at="2026-11-17T09:30:00.000Z")

    assert _poller(service, store, port).poll_once() == 0
    assert store.list_telegram_receipts() == ()


def test_a_not_configured_port_backs_off_logs_once_and_skips_heartbeats(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from oft.application import telegram_poller as poller_module
    from oft.ports.telegram import TELEGRAM_NOT_CONFIGURED, TelegramError

    class _Recorder:
        def __init__(self) -> None:
            self.info_calls: list[tuple[str, dict[str, Any]]] = []
            self.warning_calls: list[str] = []
            self.exception_calls: list[str] = []

        def info(self, event: str, **kwargs: Any) -> None:
            self.info_calls.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            self.warning_calls.append(event)

        def exception(self, event: str, **kwargs: Any) -> None:
            self.exception_calls.append(event)

    service, store, _port, _settings = _service(tmp_path)

    def _not_configured():  # type: ignore[no-untyped-def]
        raise TelegramError(TELEGRAM_NOT_CONFIGURED, "not configured")

    poller = TelegramPoller(
        service=service,
        store=store,
        clock=FixedClock(NOW),
        port_resolver=_not_configured,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )
    recorder = _Recorder()
    monkeypatch.setattr(poller_module, "LOGGER", recorder)

    assert poller.poll_once() == 0
    assert poller.poll_once() == 0

    # Unconfigured is a state, not a per-second failure: log once, idle slowly,
    # and never write a heartbeat the health check would ignore.
    assert [event for event, _ in recorder.info_calls] == ["telegram_poll_unconfigured"]
    assert recorder.warning_calls == []
    assert poller._wait_override == poller_module.UNCONFIGURED_BACKOFF_SECONDS  # noqa: SLF001
    assert store.get_system_state("telegram.poller_heartbeat") is None


def test_a_handling_failure_does_not_advance_the_offset(tmp_path: Path) -> None:
    from oft.ports.store import ConfigRecord

    service, store, port, _settings = _service(tmp_path)
    store.set_config(
        record=ConfigRecord(
            key="telegram.chat_ids",
            value_json="not-json",
            updated_at="2026-11-17T09:30:00.000Z",
            updated_by="test",
        )
    )
    port.script_updates([make_update(1)])

    assert _poller(service, store, port).poll_once() == 0
    assert store.get_system_state("telegram.poll_offset") is None


def test_an_invalid_persisted_offset_reads_as_none(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    store.set_system_state(
        "telegram.poll_offset", "not-an-int", updated_at="2026-11-17T09:30:00.000Z"
    )

    assert _poller(service, store, port).offset() is None


def test_starting_an_already_running_poller_is_idempotent(tmp_path: Path) -> None:
    service, store, port, _settings = _service(tmp_path)
    poller = _poller(service, store, port)

    poller.start()
    first = poller._thread  # noqa: SLF001 - the thread identity matters here
    poller.start()

    assert poller._thread is first  # noqa: SLF001 - no second thread was spawned
    poller.shutdown(wait=True)


def test_the_refusal_chat_map_is_bounded(tmp_path: Path) -> None:
    service, store, _port, _settings = _service(tmp_path)
    service._max_refusal_chats = 1  # noqa: SLF001 - exercise the eviction branch

    service.handle_update(parse_update(make_update(1, chat_id=9999)))
    service.handle_update(parse_update(make_update(2, chat_id=8888)))

    assert len(store.list_telegram_receipts()) == 2
    assert list(service._refusals) == [8888]  # noqa: SLF001 - the oldest entry was evicted


def _health_components(tmp_path: Path, *, token: str | None = None) -> tuple[Settings, SqliteStore]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        telegram_bot_token=token,
    )
    store = SqliteStore(settings)
    store.initialize()
    return settings, store


def _telegram_health(settings: Settings, store: SqliteStore, *, daemon_state: str = "UP") -> Any:
    from oft.application.health import DaemonProbe, build_doctor

    report = build_doctor(
        settings,
        store,
        FixtureMarketData(fixtures_dir=settings.fixtures_dir),
        FixedClock(NOW),
        daemon=DaemonProbe(state=daemon_state, detail="test listener"),  # type: ignore[arg-type]
    )
    return {check.name: check for check in report.checks}["telegram"]


def test_health_reports_unconfigured_telegram_as_ok(tmp_path: Path) -> None:
    settings, store = _health_components(tmp_path)

    check = _telegram_health(settings, store)

    assert check.status == "ok"
    assert "not configured" in check.detail


def test_health_reports_a_configured_channel_with_no_allowlist_as_degraded(
    tmp_path: Path,
) -> None:
    settings, store = _health_components(tmp_path, token=SENTINEL_TOKEN)

    check = _telegram_health(settings, store)

    assert check.status == "degraded"
    assert "no chat allowlist" in check.detail


def test_health_reports_a_chat_only_allowlist_as_degraded(tmp_path: Path) -> None:
    settings, store = _health_components(tmp_path, token=SENTINEL_TOKEN)
    SettingsService(
        store=store, clock=FixedClock(NOW), settings=settings
    ).update_telegram_allowlist([1001], [], actor="test")

    check = _telegram_health(settings, store)

    assert check.status == "degraded"
    assert "no user allowlist" in check.detail


def test_health_reports_a_ready_channel_as_ok(tmp_path: Path) -> None:
    settings, store = _health_components(tmp_path, token=SENTINEL_TOKEN)
    SettingsService(
        store=store, clock=FixedClock(NOW), settings=settings
    ).update_telegram_allowlist([1001], [2002], actor="test")
    store.set_system_state(POLLER_HEARTBEAT_KEY, "2026-11-17T09:30:00.000Z", updated_at=NOW_TEXT)

    check = _telegram_health(settings, store)

    assert check.status == "ok"
    assert "poller" in check.detail


def test_health_reports_a_stale_poller_heartbeat_as_degraded(tmp_path: Path) -> None:
    settings, store = _health_components(tmp_path, token=SENTINEL_TOKEN)
    SettingsService(
        store=store, clock=FixedClock(NOW), settings=settings
    ).update_telegram_allowlist([1001], [2002], actor="test")
    store.set_system_state(
        POLLER_HEARTBEAT_KEY,
        "2026-11-17T09:28:00.000Z",
        updated_at="2026-11-17T09:28:00.000Z",
    )

    check = _telegram_health(settings, store)

    assert check.status == "degraded"
    assert "stale poller heartbeat" in check.detail


def test_health_reports_an_unreadable_allowlist_as_degraded(tmp_path: Path) -> None:
    settings, store = _health_components(tmp_path, token=SENTINEL_TOKEN)
    store.set_config(
        record=ConfigRecord(
            key=TELEGRAM_CHAT_IDS_KEY,
            value_json="not-json",
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )

    check = _telegram_health(settings, store)

    assert check.status == "degraded"
    assert "unreadable" in check.detail


def test_an_unknown_chat_with_prose_embedded_status_gets_only_the_refusal(
    tmp_path: Path,
) -> None:
    service, store, port, _settings = _service(tmp_path)
    port.script_updates([make_update(1, chat_id=9999, user_id=2002, text="please run /status now")])

    assert _poller(service, store, port).poll_once() == 1

    receipts = store.list_telegram_receipts()
    assert len(receipts) == 1
    assert receipts[0].kind == "refusal"
    assert json.loads(receipts[0].payload_json)["text"] == REFUSAL_TEXT
    assert "version:" not in receipts[0].payload_json
    audits = [event.event_type for event in store.list_audit_events_ascending()]
    assert audits.count("TELEGRAM_UNAUTHORIZED") == 1
