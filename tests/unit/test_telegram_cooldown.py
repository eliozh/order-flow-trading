"""Durable alert cooldown/dedupe and freshness degradation transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from structlog.testing import capture_logs

from oft.application.freshness import FreshnessGuard
from oft.application.notifications import NotificationFreshnessSink, NotificationService
from oft.application.telegram_events import (
    build_degradation_event,
    build_risk_alert_event,
    enqueue_risk_alert,
)
from oft.clock import FixedClock
from oft.domain.telegram import RISK_PUSH_KEY

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-11-17T09:30:00.000Z"


class _DurableStore:
    """The persisted slices ``NotificationService`` uses, shared across restarts."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.outbox: dict[str, Any] = {}

    def get_system_state(self, key: str) -> str | None:
        return self.state.get(key)

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        self.state[key] = value

    def append_telegram_outbox(self, record: Any) -> bool:
        if record.event_key in self.outbox:
            return False
        self.outbox[record.event_key] = record
        return True

    def get_telegram_outbox(self, event_key: str) -> Any:
        return self.outbox.get(event_key)


class _Channel:
    def status_state(self) -> object:
        return type("State", (), {"state": "READY"})()

    def allowlist(self) -> object:
        return type("Allowlist", (), {"chat_ids": (1001,)})()


class _Settings:
    def __init__(self, *, risk: bool = True, position: bool = True) -> None:
        self.risk = risk
        self.position = position

    def read_scan_report_push(self) -> bool:
        return True

    def read_failed_scan_alert(self) -> bool:
        return False

    def read_trigger_alert(self) -> bool:
        return True

    def read_entry_recheck_alert(self) -> bool:
        return True

    def read_position_push(self) -> bool:
        return self.position

    def read_risk_push(self) -> bool:
        return self.risk


def _service(store: _DurableStore, settings: _Settings | None = None) -> NotificationService:
    return NotificationService(
        store=store,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        channel=_Channel(),  # type: ignore[arg-type]
        settings_service=settings or _Settings(),  # type: ignore[arg-type]
    )


def _risk_event(period: str = "2026-11-17") -> Any:
    return build_risk_alert_event(
        kind="DAILY_LOSS",
        subject="ACCOUNT",
        binding_cap="DAILY_LOSS",
        state="BREACH",
        observed="120",
        threshold="80",
        unit="USDT",
        period=period,
        reason_code="DAILY_LOSS_BREACH",
        occurred_at=NOW_TEXT,
    )


def test_repeated_tick_in_one_period_is_a_noop_and_a_new_period_alerts() -> None:
    store = _DurableStore()
    service = _service(store)

    first = service.enqueue_cooled(
        _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
    )
    second = service.enqueue_cooled(
        _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
    )
    assert first is not None
    assert second is None
    assert len(store.outbox) == 1
    assert store.state["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT"] == "2026-11-17"

    next_day = service.enqueue_cooled(
        _risk_event("2026-11-18"),
        cooldown_key="risk:DAILY_LOSS:ACCOUNT",
        period="2026-11-18",
    )
    assert next_day is not None
    assert len(store.outbox) == 2


def test_cooldown_state_survives_a_restart() -> None:
    store = _DurableStore()
    _service(store).enqueue_cooled(
        _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
    )

    # A new service instance over the same durable store is a restart.
    restarted = _service(store).enqueue_cooled(
        _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
    )

    assert restarted is None
    assert len(store.outbox) == 1


def test_a_disabled_toggle_writes_no_row_and_no_cooldown_marker() -> None:
    store = _DurableStore()
    service = _service(store, _Settings(risk=False))

    assert (
        service.enqueue_cooled(
            _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
        )
        is None
    )
    assert store.outbox == {}
    assert "telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT" not in store.state


def test_a_store_failure_is_swallowed_at_the_alert_boundary() -> None:
    class _BrokenStore(_DurableStore):
        def get_system_state(self, key: str) -> str | None:
            raise RuntimeError("system_state unavailable")

    service = _service(_BrokenStore())

    assert (
        service.enqueue_cooled(
            _risk_event(), cooldown_key="risk:DAILY_LOSS:ACCOUNT", period="2026-11-17"
        )
        is None
    )


class _RecordingFreshnessSink:
    def __init__(self) -> None:
        self.transitions: list[dict[str, Any]] = []

    def record_transition(
        self, *, stream: str, state: str, occurred_at: str, reason: str | None
    ) -> None:
        self.transitions.append(
            {"stream": stream, "state": state, "occurred_at": occurred_at, "reason": reason}
        )


def test_stale_and_recovered_transitions_alert_once_and_still_log() -> None:
    clock = FixedClock(NOW)
    sink = _RecordingFreshnessSink()
    guard = FreshnessGuard(clock=clock, alert_sink=sink)
    guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))

    with capture_logs() as logs:
        guard.evaluate(now=NOW)
        # A repeated evaluation at the same stale state must not alert again.
        guard.evaluate(now=NOW)

    assert [item["state"] for item in sink.transitions] == ["DEGRADED"]
    assert any(entry["event"] == "market_data_stale" for entry in logs)

    guard.record("WS:BTCUSDT", NOW)
    with capture_logs() as logs:
        guard.evaluate(now=NOW + timedelta(seconds=1))

    assert [item["state"] for item in sink.transitions] == ["DEGRADED", "RECOVERED"]
    assert any(entry["event"] == "market_data_fresh" for entry in logs)


def test_degradation_event_dedupe_uses_the_episode() -> None:
    store = _DurableStore()
    service = _service(store)
    degraded = build_degradation_event(
        subject="provider:binance",
        state="DEGRADED",
        reason_code="RATE_LIMITED_429",
        occurred_at=NOW_TEXT,
        episode=NOW_TEXT,
    )

    assert service.enqueue_event(degraded) is not None
    assert service.enqueue_event(degraded) is not None  # duplicate returns stored row
    assert len(store.outbox) == 1
    assert json.loads(next(iter(store.outbox.values())).payload_json)["text"]


def test_risk_toggle_key_is_registered() -> None:
    assert RISK_PUSH_KEY == "telegram.risk"


def test_approach_then_breach_each_alert_once_in_one_period() -> None:
    store = _DurableStore()
    service = _service(store)

    approach = build_risk_alert_event(
        kind="DAILY_LOSS",
        subject="ACCOUNT",
        binding_cap="DAILY_LOSS",
        state="APPROACH",
        observed="85",
        threshold="100",
        unit="USDT",
        period="2026-11-17",
        reason_code="DAILY_LOSS_APPROACH",
        occurred_at=NOW_TEXT,
    )
    breach = build_risk_alert_event(
        kind="DAILY_LOSS",
        subject="ACCOUNT",
        binding_cap="DAILY_LOSS",
        state="BREACH",
        observed="120",
        threshold="100",
        unit="USDT",
        period="2026-11-17",
        reason_code="DAILY_LOSS_BREACH",
        occurred_at=NOW_TEXT,
    )

    assert (
        enqueue_risk_alert(
            service,
            kind="DAILY_LOSS",
            subject="ACCOUNT",
            binding_cap="DAILY_LOSS",
            state="APPROACH",
            observed="85",
            threshold="100",
            unit="USDT",
            period="2026-11-17",
            reason_code="DAILY_LOSS_APPROACH",
            occurred_at=NOW_TEXT,
        )
        is not None
    )
    assert (
        enqueue_risk_alert(
            service,
            kind="DAILY_LOSS",
            subject="ACCOUNT",
            binding_cap="DAILY_LOSS",
            state="BREACH",
            observed="120",
            threshold="100",
            unit="USDT",
            period="2026-11-17",
            reason_code="DAILY_LOSS_BREACH",
            occurred_at=NOW_TEXT,
        )
        is not None
    )
    repeated = enqueue_risk_alert(
        service,
        kind="DAILY_LOSS",
        subject="ACCOUNT",
        binding_cap="DAILY_LOSS",
        state="BREACH",
        observed="120",
        threshold="100",
        unit="USDT",
        period="2026-11-17",
        reason_code="DAILY_LOSS_BREACH",
        occurred_at=NOW_TEXT,
    )

    assert repeated is None
    assert approach.dedupe_key != breach.dedupe_key
    assert len(store.outbox) == 2
    assert (
        store.state["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT:2026-11-17:APPROACH"]
        == "2026-11-17"
    )
    assert (
        store.state["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT:2026-11-17:BREACH"]
        == "2026-11-17"
    )


def test_degradation_state_survives_a_restart_mid_episode() -> None:
    store = _DurableStore()
    clock = FixedClock(NOW)

    first = NotificationFreshnessSink(_service(store))
    stale_guard = FreshnessGuard(clock=clock, alert_sink=first)
    stale_guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    stale_guard.evaluate(now=NOW)
    assert len(store.outbox) == 1

    # A restart constructs a new service and guard over the same durable store.
    restarted = NotificationFreshnessSink(_service(store))
    restarted_guard = FreshnessGuard(clock=clock, alert_sink=restarted)
    restarted_guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    restarted_guard.evaluate(now=NOW)
    assert len(store.outbox) == 1
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED"

    restarted_guard.record("WS:BTCUSDT", NOW)
    restarted_guard.evaluate(now=NOW + timedelta(seconds=1))
    assert len(store.outbox) == 2
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED"
