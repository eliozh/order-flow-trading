"""End-to-end: every Telegram command, refusal, and push over a fake transport.

One in-process composition root per test (`OFT_ENV=test` selects
``TelegramFake``), the real ``TelegramService`` / poller / outbox dispatcher,
and the real notification builders. No socket is opened and no wall-clock wait
is used. The committed fixtures under ``tests/fixtures/telegram`` supply every
inbound update and the seeded read models.
"""

from __future__ import annotations

import re
import socket
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from telegram_channel_support import (
    NOW,
    SENTINEL_TOKEN,
    Channel,
    build_channel,
    load_read_model,
    load_updates,
    seed_read_models,
)

import oft.application.secrets_service as secrets_module
from fakes.telegram import MemorySecretBackend
from oft.adapters.telegram_fake import TelegramFake
from oft.application import telegram_queries
from oft.application import telegram_service as service_module
from oft.application.health import DaemonProbe
from oft.application.telegram_dispatch import OutboxDispatcher
from oft.application.telegram_events import (
    ScanSummary,
    build_entry_recheck_event,
    build_opportunity_burst_event,
    build_opportunity_invalidated_event,
    build_opportunity_triggered_event,
    build_position_management_event,
    build_scan_summary_event,
    enqueue_risk_alert,
)
from oft.application.telegram_poller import TelegramPoller
from oft.application.telegram_watchdog import run_watchdog_once
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.opportunities import (
    Condition,
    Opportunity,
    OpportunityState,
    StateHistoryEntry,
)
from oft.domain.telegram import (
    REFUSAL_TEXT,
    SCAN_FAILED_ALERT_KEY,
    WRITE_REFUSAL_TEXT,
    parse_update,
)
from oft.domain.trigger import ConditionResult
from oft.ports.store import ConfigRecord

NOW_INSTANT = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
CONTROL_EXEMPT = frozenset({"\n", "\t"})
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
TIMESTAMP_KEYS = (
    "generated_at:",
    "mark_as_of:",
    "created_at:",
    "triggered_at:",
    "invalidated_at:",
    "occurred_at:",
)
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, emoticons, transport, supplemental
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows
    (0xFE00, 0xFE0F),  # variation selectors
    (0x1F1E6, 0x1F1FF),  # regional-indicator flags
)
_ASCII_ART_RANGES = (
    (0x2500, 0x257F),  # box drawing
    (0x2580, 0x259F),  # block elements
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to open a socket."""

    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the channel journey must never open a socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture()
def channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Channel]:
    memory = MemorySecretBackend()
    monkeypatch.setattr(secrets_module, "default_backend", lambda _settings: memory)
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    built = build_channel(settings, FixedClock(NOW_INSTANT))
    seed_read_models(built)
    built.configure()
    try:
        yield built
    finally:
        built.close()


def _poller(channel: Channel) -> TelegramPoller:
    return TelegramPoller(
        service=channel.container.telegram_service,
        store=channel.store,
        clock=channel.container.clock,
        port_resolver=lambda: channel.port,
        long_poll_timeout=0,
        poll_interval_seconds=0.01,
    )


def _dispatcher(channel: Channel) -> OutboxDispatcher:
    # A large local burst keeps this journey about one receipt per event; the
    # token bucket itself has dedicated injected-clock tests.
    return OutboxDispatcher(
        store=channel.store,
        clock=channel.container.clock,
        port_resolver=lambda: channel.port,
        jitter=lambda: 0.0,
        chat_burst=100.0,
        chat_refill_per_second=1.0,
    )


def _receipts(channel: Channel) -> dict[str, Any]:
    return {record.event_key: record for record in channel.store.list_telegram_receipts(limit=100)}


def _text(record: Any) -> str:
    import json

    return json.loads(record.payload_json)["text"]


def _has_control_characters(text: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") and character not in CONTROL_EXEMPT
        for character in text
    )


def _has_emoji(text: str) -> bool:
    for character in text:
        point = ord(character)
        if unicodedata.category(character) == "So":
            return True
        if any(low <= point <= high for low, high in _EMOJI_RANGES):
            return True
    return False


def _has_ascii_art(text: str) -> bool:
    return any(
        any(low <= ord(character) <= high for low, high in _ASCII_ART_RANGES) for character in text
    )


def _assert_plain(text: str) -> None:
    assert not _has_control_characters(text)
    assert not _has_emoji(text)
    assert not _has_ascii_art(text)
    assert SENTINEL_TOKEN not in text
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://"):
        assert forbidden not in text


def _assert_timestamps_are_iso_utc(text: str) -> None:
    for line in text.splitlines():
        for key in TIMESTAMP_KEYS:
            if line.startswith(key):
                value = line[len(key) :].strip()
                assert ISO_UTC.fullmatch(value), (key, value)


COMMAND_FIRST_LINES = {
    "status": "STATUS",
    "positions": "POSITIONS",
    "opportunities": "OPPORTUNITIES",
    "report_latest": "REPORT",
    "report_symbol": "REPORT",
    "budget": "BUDGET",
    "watchlist": "WATCHLIST",
    "help": "HELP",
}
COMMAND_UNITS = {
    "positions": "USDT",
    "opportunities": "USDT",
    "budget": "USDT",
}


def test_every_f11_command_has_one_durable_sent_receipt(channel: Channel) -> None:
    commands = load_updates()["commands"]
    updates = [item["update"] for item in commands]
    channel.port.script_updates(updates)

    assert _poller(channel).poll_once() == len(updates)
    assert _dispatcher(channel).dispatch_once() == len(updates)

    assert isinstance(channel.port, TelegramFake)
    receipts = _receipts(channel)
    assert len(receipts) == len(commands)
    for item in commands:
        update_id = item["update"]["update_id"]
        event_key = f"update:{update_id}:reply"
        receipt = receipts[event_key]
        assert receipt.state == "sent"
        assert receipt.attempts == 0
        assert receipt.message_id is not None
        assert receipt.sent_at == NOW
        text = _text(receipt)
        _assert_plain(text)
        _assert_timestamps_are_iso_utc(text)
        assert text.splitlines()[0] == COMMAND_FIRST_LINES[item["name"]]
        unit = COMMAND_UNITS.get(item["name"])
        if unit is not None:
            assert unit in text

    assert len(channel.port.sent) == len(commands)
    sent_texts = {message["text"] for message in channel.port.sent}
    assert sent_texts == {_text(receipt) for receipt in receipts.values()}
    assert {message["message_id"] for message in channel.port.sent} == {
        receipt.message_id for receipt in receipts.values()
    }


def test_every_write_like_token_only_enqueues_the_fixed_refusal(channel: Channel) -> None:
    items = load_updates()["write_like"]
    updates = [item["update"] for item in items]
    channel.port.script_updates(updates)

    assert _poller(channel).poll_once() == len(updates)
    assert _dispatcher(channel).dispatch_once() == len(updates)

    receipts = _receipts(channel)
    assert len(receipts) == len(items)
    for item in items:
        receipt = receipts[f"update:{item['update']['update_id']}:reply"]
        assert receipt.kind == "refusal"
        assert receipt.state == "sent"
        assert _text(receipt) == WRITE_REFUSAL_TEXT


def test_prose_fake_commands_and_injection_shapes_are_only_data(channel: Channel) -> None:
    items = load_updates()["injection"]
    channel.port.script_updates([item["update"] for item in items])

    assert _poller(channel).poll_once() == len(items)
    _dispatcher(channel).dispatch_once()
    receipts = _receipts(channel)

    expected_ids = {item["update"]["update_id"] for item in items}
    assert {int(key.split(":")[1]) for key in receipts} <= expected_ids
    for item in items:
        update_id = item["update"]["update_id"]
        event_key = f"update:{update_id}:reply"
        if item["expected"] == "ignored":
            assert event_key not in receipts
            continue
        receipt = receipts[event_key]
        text = _text(receipt)
        _assert_plain(text)
        if item["expected"] == "refusal":
            assert receipt.kind == "refusal"
            assert text == WRITE_REFUSAL_TEXT
            continue
        assert receipt.kind == "query_reply"
        first_line = text.splitlines()[0]
        assert first_line in {
            "STATUS",
            "POSITIONS",
            "查询失败：参数无效。请重试或查看桌面端。",
        }


@pytest.mark.parametrize(
    "item",
    load_updates()["authorization"],
    ids=lambda item: item["name"],
)
def test_unauthorized_update_is_refused_before_any_read(
    channel: Channel, monkeypatch: pytest.MonkeyPatch, item: dict[str, Any]
) -> None:
    # Apply the fixture's per-item allowlist so the three cases exercise
    # distinct checks instead of being refused by an empty user list alone:
    # unknown chat (default), unknown user in an allowlisted chat (default),
    # and chat-only allowlist (chat allowed, no user allowed).
    allowlist = load_updates()["allowlists"][item["allowlist"]]
    channel.configure(
        chat_ids=allowlist["chat_ids"],
        user_ids=allowlist["allowed_user_ids"],
    )
    calls: list[str] = []

    def _forbidden_read(name: str) -> Any:
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            pytest.fail(f"an unauthorized update reached the {name} read model")

        return _fail

    for name in (
        "render_positions_query",
        "render_opportunities_query",
        "render_report_query",
        "render_budget_query",
        "render_watchlist_query",
    ):
        monkeypatch.setattr(telegram_queries, name, _forbidden_read(name))
    for name in ("build_status", "render_status", "render_help"):
        monkeypatch.setattr(service_module, name, _forbidden_read(name))

    channel.port.script_updates([item["update"]])
    assert _poller(channel).poll_once() == 1
    assert _dispatcher(channel).dispatch_once() == 1

    assert calls == []
    receipt = _receipts(channel)[f"update:{item['update']['update_id']}:reply"]
    assert receipt.kind == "refusal"
    assert _text(receipt) == REFUSAL_TEXT
    assert "version:" not in receipt.payload_json
    assert "symbols:" not in receipt.payload_json

    audits = [
        event.event_type
        for event in channel.store.list_audit_events_ascending()
        if event.event_type == "TELEGRAM_UNAUTHORIZED"
    ]
    assert audits


def test_the_default_allowlist_authorizes_the_allowlisted_operator(channel: Channel) -> None:
    # Contrast for the refusal cases: the same fixture allowlist data admits a
    # known chat and user, so the refusals above are not an artifact of a
    # configuration that refuses every sender.
    allowlist = load_updates()["allowlists"]["default"]
    channel.configure(
        chat_ids=allowlist["chat_ids"],
        user_ids=allowlist["allowed_user_ids"],
    )
    update = {
        "update_id": 900,
        "message": {
            "message_id": 901,
            "chat": {"id": allowlist["chat_ids"][0], "type": "private"},
            "from": {"id": allowlist["allowed_user_ids"][0]},
            "text": "/status",
        },
    }
    channel.port.script_updates([update])

    assert _poller(channel).poll_once() == 1
    assert _dispatcher(channel).dispatch_once() == 1

    receipt = _receipts(channel)["update:900:reply"]
    assert receipt.kind == "query_reply"
    assert _text(receipt).splitlines()[0] == "STATUS"


def test_ignored_updates_produce_no_receipt_and_no_read_model_access(
    channel: Channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel.configure()
    calls: list[str] = []

    def _forbidden_read(name: str) -> Any:
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            pytest.fail(f"an ignored update reached the {name} read model")

        return _fail

    for name in (
        "render_positions_query",
        "render_opportunities_query",
        "render_report_query",
        "render_budget_query",
        "render_watchlist_query",
    ):
        monkeypatch.setattr(telegram_queries, name, _forbidden_read(name))
    for name in ("build_status", "render_status", "render_help"):
        monkeypatch.setattr(service_module, name, _forbidden_read(name))

    items = load_updates()["ignored"]
    channel.port.script_updates([item["update"] for item in items])
    # A non-text item is dropped before dispatch, so count only parsed updates.
    parsed = sum(1 for item in items if parse_update(item["update"]) is not None)
    assert _poller(channel).poll_once() == parsed

    assert calls == []
    assert channel.store.list_telegram_receipts() == ()
    audit_types = [event.event_type for event in channel.store.list_audit_events_ascending()]
    assert "TELEGRAM_UNAUTHORIZED" not in audit_types


def _opportunity(state: OpportunityState, history_length: int) -> Opportunity:
    history = tuple(
        StateHistoryEntry(
            from_state=None if index == 0 else OpportunityState.OPEN,
            to_state=state if index == history_length - 1 else OpportunityState.OPEN,
            at=NOW,
            reason_code="CREATED" if index == 0 else "TRIGGER_MET",
            note="",
        )
        for index in range(history_length)
    )
    return Opportunity(
        opportunity_id="OPP-TG-1",
        symbol="BTCUSDT",
        side="LONG",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        zone_low=Decimal("69000"),
        zone_high=Decimal("70000"),
        entry_preference="LIMIT",
        trigger_conditions=(),
        invalidation_conditions=(
            Condition(
                condition_id="C2",
                draft_id="D2",
                type="PRICE_BELOW",
                params={"price": "69000"},
                window_start=NOW,
                window_end=NOW,
                required_data=(),
                evaluable=True,
            ),
        ),
        expires_at="2026-11-18T00:00:00.000Z",
        thesis="Range high retest.",
        confidence=0.72,
        risk_request={"entry_price": "70000", "stop_price": "69000"},
        source={"scan_id": "SCAN-TG-1", "report_id": "RPT-TG-1", "decision_id": "DEC-TG-1"},
        provenance={},
        data_quality={"status": "COMPLETE", "reasons": []},
        state_history=history,
    )


def _trigger_result() -> ConditionResult:
    return ConditionResult(
        condition_id="C1",
        type="PRICE_ABOVE",
        met=True,
        observed_value="70500",
        threshold="70000",
        evaluated_at=NOW,
        data_quality="COMPLETE",
        evaluable=True,
        note="",
    )


def _emitted_pushes(channel: Channel) -> list[tuple[str, Any]]:
    container = channel.container
    notifications = container.notifications
    channel.store.set_config(
        ConfigRecord(
            key=SCAN_FAILED_ALERT_KEY,
            value_json="true",
            updated_at=NOW,
            updated_by="test",
        )
    )
    emitted: list[tuple[str, Any]] = []

    complete = build_scan_summary_event(
        ScanSummary(
            scan_id="SCAN-TG-1",
            status="COMPLETE",
            trigger_source="MANUAL",
            period="4h",
            symbols_total=1,
            symbols_completed=1,
            symbols_degraded=0,
            symbols_failed=0,
            new_opportunities=1,
            cost_usd="0.10",
            cost_unpriced=False,
            report_ids=("RPT-TG-1",),
            reports_total=1,
            failed_stage=None,
            reason_code=None,
            generated_at=NOW,
        )
    )
    emitted.append(("scan_summary", notifications.enqueue_event(complete)))

    failed = build_scan_summary_event(
        ScanSummary(
            scan_id="SCAN-TG-FAILED",
            status="FAILED",
            trigger_source="SCHEDULED",
            period="1h",
            symbols_total=1,
            symbols_completed=0,
            symbols_degraded=0,
            symbols_failed=1,
            new_opportunities=0,
            cost_usd="N/A",
            cost_unpriced=True,
            report_ids=(),
            reports_total=0,
            failed_stage="DATA",
            reason_code="DATA_UNAVAILABLE",
            generated_at=NOW,
        )
    )
    emitted.append(("scan_summary", notifications.enqueue_event(failed)))

    triggered = build_opportunity_triggered_event(
        _opportunity(OpportunityState.TRIGGERED, 2),
        trigger_id="TRG-TG-1",
        triggered_at=NOW,
        results=(_trigger_result(),),
    )
    emitted.append(("opportunity_triggered", notifications.enqueue_event(triggered)))
    emitted.append(
        (
            "opportunity_invalidated",
            notifications.enqueue_event(
                build_opportunity_invalidated_event(
                    _opportunity(OpportunityState.INVALIDATED, 3),
                    reason_code="PRICE_BELOW_ZONE",
                    invalidated_at=NOW,
                )
            ),
        )
    )
    emitted.append(
        (
            "opportunity_triggered_burst",
            notifications.enqueue_event(build_opportunity_burst_event([triggered])),
        )
    )
    emitted.append(
        (
            "entry_recheck",
            notifications.enqueue_event(
                build_entry_recheck_event(
                    trigger_id="TRG-TG-1",
                    opportunity_id="OPP-TG-1",
                    symbol="BTCUSDT",
                    side="LONG",
                    decision_id="DEC-TG-1",
                    decision="ENTER_LONG",
                    confidence=0.72,
                    generated_at=NOW,
                )
            ),
        )
    )
    emitted.append(
        (
            "position_management",
            notifications.enqueue_event(
                build_position_management_event(
                    position_id="POS-TG-1",
                    symbol="BTCUSDT",
                    action="PARTIAL_CLOSE",
                    status="EXECUTED",
                    size="0.005",
                    price="70500",
                    reason_code="PARTIAL_CLOSE",
                    decision_id="DEC-TG-1",
                    occurred_at=NOW,
                )
            ),
        )
    )
    emitted.append(
        (
            "risk_alert",
            enqueue_risk_alert(
                notifications,
                kind="DAILY_LOSS",
                subject="ACCOUNT",
                binding_cap="DAILY_LOSS",
                state="BREACH",
                observed="320",
                threshold="300",
                unit="USDT",
                period="2026-11-17",
                reason_code="DAILY_LOSS_BREACH",
                occurred_at=NOW,
            ),
        )
    )
    emitted.append(
        (
            "budget_threshold",
            notifications.enqueue_budget_threshold(
                period="day",
                period_key="2026-11-17",
                spend_usd="8",
                threshold_usd="10",
                state="WARN",
                occurred_at=NOW,
            ),
        )
    )
    emitted.append(
        (
            "degradation",
            notifications.enqueue_degradation_transition(
                subject="MARKET_DATA",
                state="DEGRADED",
                reason_code="WS_DISCONNECTED",
                occurred_at=NOW,
            ),
        )
    )
    emitted.append(
        (
            "degradation",
            notifications.enqueue_degradation_transition(
                subject="MARKET_DATA",
                state="RECOVERED",
                reason_code="WS_RECONNECTED",
                occurred_at=NOW,
            ),
        )
    )
    return emitted


def test_every_push_event_enqueues_one_receipt_and_one_sent_message(channel: Channel) -> None:
    emitted = _emitted_pushes(channel)
    assert all(record is not None for _kind, record in emitted), [
        kind for kind, record in emitted if record is None
    ]
    assert len(channel.store.list_telegram_receipts(limit=100)) == len(emitted)

    # The dispatcher claims a bounded batch per tick; two ticks drain the set.
    dispatched = _dispatcher(channel).dispatch_once() + _dispatcher(channel).dispatch_once()
    assert dispatched == len(emitted)
    assert _dispatcher(channel).dispatch_once() == 0

    receipts = _receipts(channel)
    assert len(receipts) == len(emitted)
    expected_first_lines = {
        "scan_summary": {"SCAN COMPLETE", "SCAN FAILED"},
        "opportunity_triggered": {"机会触发 · BTCUSDT · LONG · 置信度 0.72"},
        "opportunity_invalidated": {"机会失效 · BTCUSDT · LONG"},
        "opportunity_triggered_burst": {"机会触发 1 个"},
        "entry_recheck": {"入场复核 · BTCUSDT · LONG · 置信度 0.72"},
        "position_management": {"持仓管理 · BTCUSDT · PARTIAL_CLOSE"},
        "risk_alert": {"风险告警 · DAILY_LOSS · BREACH"},
        "budget_threshold": {"预算告警 · DAY · WARN"},
        "degradation": {"系统降级 · MARKET_DATA", "系统恢复 · MARKET_DATA"},
    }
    seen: set[str] = set()
    push_units = {
        "scan_summary": ("USDT",),
        "opportunity_triggered": ("USDT",),
        "position_management": ("USDT",),
        "risk_alert": ("USDT",),
        "budget_threshold": ("USDT", "%"),
    }
    for record in receipts.values():
        assert record.state == "sent"
        assert record.attempts == 0
        assert record.message_id is not None
        text = _text(record)
        _assert_plain(text)
        _assert_timestamps_are_iso_utc(text)
        first_line = text.splitlines()[0]
        assert first_line in expected_first_lines[record.kind]
        for unit in push_units.get(record.kind, ()):
            assert unit in text
        seen.add(first_line)
    assert seen == set().union(*expected_first_lines.values())
    assert len(channel.port.sent) == len(emitted)


def test_watchdog_alert_and_recovery_use_the_fake_port(channel: Channel, tmp_path: Path) -> None:
    probe_state = {"state": "OFFLINE"}

    def _probe() -> DaemonProbe:
        return DaemonProbe(state=probe_state["state"], detail="test daemon down")  # type: ignore[arg-type]

    first = run_watchdog_once(
        store=channel.store,
        clock=channel.container.clock,
        probe=_probe,
        port=channel.port,
        chat_id=1001,
        lock_path=tmp_path / "watchdog.lock",
    )
    repeated = run_watchdog_once(
        store=channel.store,
        clock=channel.container.clock,
        probe=_probe,
        port=channel.port,
        chat_id=1001,
        lock_path=tmp_path / "watchdog.lock",
    )
    probe_state["state"] = "UP"
    recovery = run_watchdog_once(
        store=channel.store,
        clock=channel.container.clock,
        probe=_probe,
        port=channel.port,
        chat_id=1001,
        lock_path=tmp_path / "watchdog.lock",
    )

    assert (first.daemon_state, first.action) == ("OFFLINE", "ALERT_SENT")
    assert repeated.action == "NONE"
    assert (recovery.daemon_state, recovery.action) == ("UP", "RECOVERY_SENT")
    assert len(channel.port.sent) == 2
    alert_text, recovery_text = channel.port.sent[0]["text"], channel.port.sent[1]["text"]
    assert alert_text.splitlines()[0] == "系统降级 · OFT 守护进程"
    assert recovery_text.splitlines()[0] == "系统恢复 · OFT 守护进程"
    assert "DAEMON_DOWN" in alert_text
    assert "DAEMON_RECOVERED" in recovery_text
    # Direct watchdog sends obey the same A12 plain-text contract.
    _assert_plain(alert_text)
    _assert_plain(recovery_text)
    # The watchdog sends directly and never fabricates an outbox receipt.
    assert channel.store.list_telegram_receipts() == ()


def test_the_seeded_read_models_render_the_documented_units(channel: Channel) -> None:
    budget = load_read_model("budget")
    command = load_updates()["commands"][5]["update"]
    channel.port.script_updates([command])
    assert _poller(channel).poll_once() == 1
    assert _dispatcher(channel).dispatch_once() == 1
    receipt = _receipts(channel)[f"update:{command['update_id']}:reply"]
    text = _text(receipt)
    thresholds = budget["thresholds"]
    assert f"daily_usd: {thresholds['daily_usd']} USDT" in text
    assert f"weekly_usd: {thresholds['weekly_usd']} USDT" in text
    assert f"monthly_usd: {thresholds['monthly_usd']} USDT" in text
    assert "warn_pct: 80 %" in text
    assert receipt.attempts == 0
    assert receipt.message_id is not None
