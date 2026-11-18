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
    enqueue_budget_threshold,
    enqueue_risk_alert,
)
from oft.clock import FixedClock
from oft.domain.telegram import BUDGET_PUSH_KEY, RISK_PUSH_KEY

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

    def list_budget_records_for_period(self, period: str, period_key: str) -> tuple[Any, ...]:
        return ()


class _Channel:
    def status_state(self) -> object:
        return type("State", (), {"state": "READY"})()

    def allowlist(self) -> object:
        return type("Allowlist", (), {"chat_ids": (1001,)})()


class _Settings:
    def __init__(self, *, risk: bool = True, position: bool = True, budget: bool = True) -> None:
        self.risk = risk
        self.position = position
        self.budget = budget

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

    def read_budget_push(self) -> bool:
        return self.budget


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


class _WarningRecorder:
    """An order-independent stand-in for a structlog logger.

    Another test may have run ``configure_logging`` and cached a stdlib logger
    (``cache_logger_on_first_use``), which makes ``capture_logs`` order
    dependent; patching the module logger avoids that.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))


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
    assert store.state["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT:APPROACH"] == "2026-11-17"
    assert store.state["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT:BREACH"] == "2026-11-17"


def _risk_kwargs(period: str) -> dict[str, Any]:
    return {
        "kind": "DAILY_LOSS",
        "subject": "ACCOUNT",
        "binding_cap": "DAILY_LOSS",
        "state": "BREACH",
        "observed": "120",
        "threshold": "100",
        "unit": "USDT",
        "period": period,
        "reason_code": "DAILY_LOSS_BREACH",
        "occurred_at": NOW_TEXT,
    }


def test_two_periods_do_not_grow_cooldown_rows_but_alert_once_each() -> None:
    store = _DurableStore()
    service = _service(store)

    first = enqueue_risk_alert(service, **_risk_kwargs("2026-11-17"))
    repeated = enqueue_risk_alert(service, **_risk_kwargs("2026-11-17"))
    next_day = enqueue_risk_alert(service, **_risk_kwargs("2026-11-18"))

    assert first is not None
    assert repeated is None
    assert next_day is not None
    assert len(store.outbox) == 2
    # One stable cooldown row per kind+subject+state, however many periods alert.
    cooldown_rows = [key for key in store.state if key.startswith("telegram.alert_cooldown.")]
    assert cooldown_rows == ["telegram.alert_cooldown.risk:DAILY_LOSS:ACCOUNT:BREACH"]
    assert store.state[cooldown_rows[0]] == "2026-11-18"


def test_budget_toggle_key_is_registered() -> None:
    assert BUDGET_PUSH_KEY == "telegram.budget"


def _budget_kwargs(period_key: str, *, state: str = "WARN") -> dict[str, Any]:
    return {
        "period": "day",
        "period_key": period_key,
        "spend_usd": "8.50",
        "threshold_usd": "10.00",
        "state": state,
        "unpriced_count": 0,
        "occurred_at": NOW_TEXT,
    }


def test_budget_alert_cooldown_is_one_row_per_period_and_state() -> None:
    store = _DurableStore()
    service = _service(store)

    first = enqueue_budget_threshold(service, **_budget_kwargs("2026-11-17"))
    repeated = enqueue_budget_threshold(service, **_budget_kwargs("2026-11-17"))
    next_day = enqueue_budget_threshold(service, **_budget_kwargs("2026-11-18"))
    exceeded = enqueue_budget_threshold(service, **_budget_kwargs("2026-11-18", state="EXCEEDED"))

    assert first is not None
    assert repeated is None
    assert next_day is not None
    assert exceeded is not None
    assert len(store.outbox) == 3
    cooldown_rows = sorted(key for key in store.state if key.startswith("telegram.alert_cooldown."))
    assert cooldown_rows == [
        "telegram.alert_cooldown.budget:day:EXCEEDED",
        "telegram.alert_cooldown.budget:day:WARN",
    ]
    assert store.state["telegram.alert_cooldown.budget:day:WARN"] == "2026-11-18"


def test_a_disabled_budget_toggle_writes_no_row_and_no_cooldown_marker() -> None:
    store = _DurableStore()
    service = _service(store, _Settings(budget=False))

    assert enqueue_budget_threshold(service, **_budget_kwargs("2026-11-17")) is None
    assert store.outbox == {}
    assert "telegram.alert_cooldown.budget:day:WARN" not in store.state


def _budget_service_kwargs(period_key: str, *, state: str = "WARN") -> dict[str, Any]:
    # The production method computes the unpriced count itself.
    values = _budget_kwargs(period_key, state=state)
    values.pop("unpriced_count")
    return values


def test_the_production_service_budget_path_is_once_per_period_across_restart() -> None:
    store = _DurableStore()

    first = _service(store).enqueue_budget_threshold(**_budget_service_kwargs("2026-11-17"))
    # A new service instance over the same durable store is a restart.
    restarted = _service(store).enqueue_budget_threshold(**_budget_service_kwargs("2026-11-17"))
    next_day = _service(store).enqueue_budget_threshold(**_budget_service_kwargs("2026-11-18"))

    assert first is not None
    assert restarted is None
    assert next_day is not None
    assert len(store.outbox) == 2
    assert store.state["telegram.alert_cooldown.budget:day:WARN"] == "2026-11-18"


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
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:1"

    restarted_guard.record("WS:BTCUSDT", NOW)
    restarted_guard.evaluate(now=NOW + timedelta(seconds=1))
    assert len(store.outbox) == 2
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED:2"


def _degradation_keys(store: _DurableStore) -> list[str]:
    return sorted(record.event_key for record in store.outbox.values())


def test_first_fresh_observation_after_restart_recovers_then_a_new_outage_degrades() -> None:
    store = _DurableStore()
    clock = FixedClock(NOW)

    first = NotificationFreshnessSink(_service(store))
    guard = FreshnessGuard(clock=clock, alert_sink=first)
    guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    guard.evaluate(now=NOW)
    assert _degradation_keys(store) == ["degraded:WS:BTCUSDT:DEGRADED:1"]

    # The outage recovered while the daemon was down, so the restarted guard's
    # first observation is fresh and must transition the stale marker once.
    restarted = NotificationFreshnessSink(_service(store))
    restarted_guard = FreshnessGuard(clock=clock, alert_sink=restarted)
    restarted_guard.record("WS:BTCUSDT", NOW)
    restarted_guard.evaluate(now=NOW)
    restarted_guard.evaluate(now=NOW)
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:RECOVERED:2",
    ]
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED:2"

    # A later, genuinely new outage emits exactly one new DEGRADED.
    restarted_guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    restarted_guard.evaluate(now=NOW)
    restarted_guard.evaluate(now=NOW)
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:DEGRADED:3",
        "degraded:WS:BTCUSDT:RECOVERED:2",
    ]
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:3"


def test_several_fresh_records_before_evaluate_attempt_one_recovery() -> None:
    store = _DurableStore()
    clock = FixedClock(NOW)

    first = NotificationFreshnessSink(_service(store))
    guard = FreshnessGuard(clock=clock, alert_sink=first)
    guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    guard.evaluate(now=NOW)
    assert _degradation_keys(store) == ["degraded:WS:BTCUSDT:DEGRADED:1"]

    # The live loop records one event at a time and evaluates only on the
    # flush/connect boundary, so several fresh records precede the first
    # evaluate after a restart. The startup recovery must survive them.
    restarted = NotificationFreshnessSink(_service(store))
    restarted_guard = FreshnessGuard(clock=clock, alert_sink=restarted)
    restarted_guard.record("WS:BTCUSDT", NOW)
    restarted_guard.record("WS:BTCUSDT", NOW + timedelta(seconds=1))
    restarted_guard.evaluate(now=NOW + timedelta(seconds=1))
    restarted_guard.evaluate(now=NOW + timedelta(seconds=2))

    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:RECOVERED:2",
    ]
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED:2"


def test_first_fresh_observation_on_a_clean_install_emits_nothing(monkeypatch: Any) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _DurableStore()
    guard = FreshnessGuard(
        clock=FixedClock(NOW),
        alert_sink=NotificationFreshnessSink(_service(store)),
    )

    guard.record("WS:BTCUSDT", NOW)
    guard.record("WS:BTCUSDT", NOW)
    guard.evaluate(now=NOW)

    assert store.outbox == {}
    assert "telegram.degradation_state.WS:BTCUSDT" not in store.state
    # A clean install is not a lost marker: nothing may claim a recovery.
    assert [
        event for event, _ in recorder.warnings if event == "telegram_degradation_marker_lost"
    ] == []


def test_a_failed_degradation_marker_write_does_not_reemit_after_restart(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    class _FailOnceMarkerStore(_DurableStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_marker_writes = 1

        def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
            if key.startswith("telegram.degradation_state.") and self.fail_marker_writes:
                self.fail_marker_writes -= 1
                raise RuntimeError("degradation state unavailable")
            super().set_system_state(key, value, updated_at=updated_at)

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _FailOnceMarkerStore()
    sink = NotificationFreshnessSink(_service(store))
    first = sink.record_transition(
        stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
    )
    assert first is not None
    assert len(store.outbox) == 1
    assert "telegram.degradation_state.WS:BTCUSDT" not in store.state
    assert [event for event, _ in recorder.warnings] == ["telegram_degradation_state_unwritable"]

    # A restart re-observes the same transition at a new instant. The marker is
    # unchanged, so the recomputed episode key is identical and the outbox
    # dedupes while the duplicate path retries (and heals) the marker write.
    restarted = NotificationFreshnessSink(_service(store))
    again = restarted.record_transition(
        stream="WS:BTCUSDT",
        state="DEGRADED",
        occurred_at="2026-11-17T09:31:00.000Z",
        reason=None,
    )
    assert again is not None
    assert len(store.outbox) == 1
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:1"

    recovered = restarted.record_transition(
        stream="WS:BTCUSDT", state="RECOVERED", occurred_at=NOW_TEXT, reason=None
    )
    later_outage = restarted.record_transition(
        stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
    )
    assert recovered is not None
    assert later_outage is not None
    assert len(store.outbox) == 3
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:DEGRADED:3",
        "degraded:WS:BTCUSDT:RECOVERED:2",
    ]


class _ScriptedMarkerStore(_DurableStore):
    """Fails the first degradation-marker write for each scripted state."""

    def __init__(self, *, fail_on: tuple[str, ...]) -> None:
        super().__init__()
        self.fail_on = set(fail_on)

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        if key.startswith("telegram.degradation_state."):
            marker_state = value.partition(":")[0]
            if marker_state in self.fail_on:
                self.fail_on.discard(marker_state)
                raise RuntimeError("degradation state unavailable")
        super().set_system_state(key, value, updated_at=updated_at)


def _marker_lost_warnings(recorder: _WarningRecorder) -> list[dict[str, Any]]:
    return [
        fields for event, fields in recorder.warnings if event == "telegram_degradation_marker_lost"
    ]


def test_a_failed_degraded_marker_write_still_emits_the_recovery_and_next_outage(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _ScriptedMarkerStore(fail_on=("DEGRADED",))
    sink = NotificationFreshnessSink(_service(store))

    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert "telegram.degradation_state.WS:BTCUSDT" not in store.state

    # The DEGRADED marker was lost, but the outbox still records the outage, so
    # this recovery is a real transition and must not be dropped as a clean install.
    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="RECOVERED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED:1"

    # The next genuine outage is a new episode, not a duplicate of DEGRADED:1.
    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:2"
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:DEGRADED:2",
        "degraded:WS:BTCUSDT:RECOVERED:1",
    ]
    assert len(_marker_lost_warnings(recorder)) == 1


def test_a_failed_recovered_marker_write_still_emits_the_next_outage(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _ScriptedMarkerStore(fail_on=("RECOVERED",))
    sink = NotificationFreshnessSink(_service(store))

    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:1"

    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="RECOVERED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    # The RECOVERED row is durable, but its marker write failed.
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:1"

    # The next genuine outage must not be mistaken for a restart re-observation.
    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:3"
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:DEGRADED:3",
        "degraded:WS:BTCUSDT:RECOVERED:2",
    ]

    # Once healed, a same-state re-observation stays silent (restart semantics).
    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is None
    )
    assert len(_marker_lost_warnings(recorder)) == 1


def test_lost_degraded_and_recovered_markers_still_emit_the_next_outage(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _ScriptedMarkerStore(fail_on=("DEGRADED", "RECOVERED"))
    sink = NotificationFreshnessSink(_service(store))

    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert "telegram.degradation_state.WS:BTCUSDT" not in store.state

    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="RECOVERED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert "telegram.degradation_state.WS:BTCUSDT" not in store.state

    # Both marker writes were lost. The queued RECOVERED proves the previous
    # episode ended, so this outage is genuinely new: it must be DEGRADED:2,
    # not a dedupe against the still-queued DEGRADED:1.
    assert (
        sink.record_transition(
            stream="WS:BTCUSDT", state="DEGRADED", occurred_at=NOW_TEXT, reason=None
        )
        is not None
    )
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "DEGRADED:2"
    assert _degradation_keys(store) == [
        "degraded:WS:BTCUSDT:DEGRADED:1",
        "degraded:WS:BTCUSDT:DEGRADED:2",
        "degraded:WS:BTCUSDT:RECOVERED:1",
    ]
    assert len(_marker_lost_warnings(recorder)) == 2


class _UnreadableOutboxStore(_DurableStore):
    """A store whose outbox probe always raises."""

    def get_telegram_outbox(self, event_key: str) -> Any:
        raise RuntimeError("outbox unavailable")


def test_an_unreadable_outbox_probe_enqueues_the_recovery_and_warns_once(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _UnreadableOutboxStore()
    sink = NotificationFreshnessSink(_service(store))

    record = sink.record_transition(
        stream="WS:BTCUSDT", state="RECOVERED", occurred_at=NOW_TEXT, reason=None
    )

    # An unreadable probe is "unknown", not "absent": the recovery must still
    # be enqueued rather than silently dropped as a clean install.
    assert record is not None
    assert _degradation_keys(store) == ["degraded:WS:BTCUSDT:RECOVERED:1"]
    assert store.state["telegram.degradation_state.WS:BTCUSDT"] == "RECOVERED:1"
    assert len(_marker_lost_warnings(recorder)) == 1
    probe_warnings = [
        fields for event, fields in recorder.warnings if event == "telegram_outbox_probe_unreadable"
    ]
    assert len(probe_warnings) == 1
    assert probe_warnings[0]["error_type"] == "RuntimeError"


class _DuplicateWithUnreadableProbeStore(_DurableStore):
    """A duplicate insert whose follow-up probe cannot be read."""

    def append_telegram_outbox(self, record: Any) -> bool:
        return False

    def get_telegram_outbox(self, event_key: str) -> Any:
        raise RuntimeError("outbox unavailable")


def test_a_duplicate_insert_with_an_unreadable_probe_still_returns_none(
    monkeypatch: Any,
) -> None:
    import oft.application.notifications as notifications_module

    recorder = _WarningRecorder()
    monkeypatch.setattr(notifications_module, "LOGGER", recorder)
    store = _DuplicateWithUnreadableProbeStore()

    assert _service(store).enqueue_event(_risk_event()) is None
    probe_warnings = [
        fields for event, fields in recorder.warnings if event == "telegram_outbox_probe_unreadable"
    ]
    assert len(probe_warnings) == 1
    assert probe_warnings[0]["error_type"] == "RuntimeError"
    assert [
        event for event, _ in recorder.warnings if event == "telegram_notification_enqueue_failed"
    ] == []


class _RaisingFreshnessSink:
    def record_transition(
        self, *, stream: str, state: str, occurred_at: str, reason: str | None
    ) -> None:
        raise RuntimeError("freshness sink unavailable")


def test_a_raising_freshness_sink_is_contained_and_evaluate_still_returns_stamps(
    monkeypatch: Any,
) -> None:
    guard = FreshnessGuard(clock=FixedClock(NOW), alert_sink=_RaisingFreshnessSink())
    recorder = _WarningRecorder()
    monkeypatch.setattr(guard, "_logger", recorder)
    guard.record("WS:BTCUSDT", NOW)

    stamps = guard.evaluate(now=NOW)

    assert len(stamps) == 1
    assert stamps[0].status.value == "FRESH"
    failures = [
        fields for event, fields in recorder.warnings if event == "freshness_alert_enqueue_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["state"] == "RECOVERED"
    assert failures[0]["error_type"] == "RuntimeError"
