"""Opportunity/entry push events: allowlisted payloads and deterministic renderers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from oft.application.notifications import NotificationService
from oft.application.telegram_events import (
    ENTRY_RECHECK_EVENT_TYPE,
    OPPORTUNITY_INVALIDATED_EVENT_TYPE,
    OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE,
    OPPORTUNITY_TRIGGERED_EVENT_TYPE,
    build_entry_recheck_event,
    build_opportunity_burst_event,
    build_opportunity_invalidated_event,
    build_opportunity_triggered_event,
    entry_alert_decision,
)
from oft.application.telegram_messages import (
    ENTRY_RECHECK_INSTRUCTION,
    render_entry_recheck_alert,
    render_invalidation_alert,
    render_opportunity_burst,
    render_trigger_alert,
)
from oft.clock import FixedClock
from oft.domain.opportunities import (
    Condition,
    Opportunity,
    OpportunityState,
    new_opportunity,
    transition,
)
from oft.domain.telegram import MAX_OPPORTUNITY_ALERT_ITEMS, MAX_OUTBOUND_CHARS
from oft.domain.trigger import ConditionResult

NOW = "2026-11-17T09:30:00.000Z"
LATER = "2026-11-17T10:00:00.000Z"
WINDOW = {"start": "2026-11-01T00:00:00.000Z", "end": "2026-12-01T00:00:00.000Z"}
SECRET = "SECRET_PROMPT_TEXT provider-raw-body sk-abcdef0123456789"


def _condition(
    condition_id: str,
    type_: str,
    params: dict[str, Any],
) -> Condition:
    return Condition(
        condition_id=condition_id,
        draft_id="d",
        type=type_,
        params=params,
        window_start=WINDOW["start"],
        window_end=WINDOW["end"],
        required_data=(),
        evaluable=True,
    )


def _open_opportunity(
    *,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    confidence: float = 0.72,
    thesis: str = SECRET,
    invalidation_conditions: tuple[Condition, ...] | None = None,
) -> Opportunity:
    return new_opportunity(
        opportunity_id="OPP-1",
        symbol=symbol,
        side=side,
        zone_low=Decimal("69000"),
        zone_high=Decimal("71000"),
        entry_preference="MARKET",
        trigger_conditions=(_condition("C-1", "PRICE_TOUCH_ZONE", {"price_low": "69900"}),),
        invalidation_conditions=invalidation_conditions
        or (_condition("I-1", "PRICE_CROSS_BELOW", {"price": "68000"}),),
        expires_at="2099-01-01T00:00:00.000Z",
        thesis=thesis,
        confidence=confidence,
        risk_request={},
        source={"report_id": "RPT-1"},
        provenance={},
        data_quality={},
        at=NOW,
    )


def _triggered(**kwargs: Any) -> Opportunity:
    return transition(
        _open_opportunity(**kwargs),
        OpportunityState.TRIGGERED,
        at=NOW,
        reason_code="TRIGGER_MET",
        note="TRG-1",
    )


def _invalidated(**kwargs: Any) -> Opportunity:
    triggered = _triggered(**kwargs)
    return transition(
        triggered,
        OpportunityState.INVALIDATED,
        at=LATER,
        reason_code="REJECTED",
        note="human reject",
    )


def _result() -> ConditionResult:
    return ConditionResult(
        condition_id="C-1",
        type="PRICE_TOUCH_ZONE",
        met=True,
        observed_value="70000",
        threshold="70100",
        evaluated_at=NOW,
        data_quality="COMPLETE",
        evaluable=True,
        note="",
    )


def _triggered_event(opportunity: Opportunity | None = None) -> Any:
    return build_opportunity_triggered_event(
        opportunity or _triggered(),
        trigger_id="TRG-1",
        triggered_at=NOW,
        results=(_result(),),
    )


def test_triggered_dedupe_key_is_per_transition_and_payload_is_allowlisted() -> None:
    event = _triggered_event()

    assert event.event_type == OPPORTUNITY_TRIGGERED_EVENT_TYPE
    assert event.dedupe_key == "opp:OPP-1:TRIGGERED:2"
    assert event.subject == {"kind": "OPPORTUNITY", "id": "OPP-1"}
    assert set(event.payload) == {
        "opportunity_id",
        "symbol",
        "side",
        "state",
        "state_version",
        "confidence",
        "zone_low",
        "zone_high",
        "trigger_id",
        "triggered_at",
        "trigger_levels",
        "invalidation_levels",
    }
    assert event.payload["state"] == "TRIGGERED"
    assert event.payload["state_version"] == 2
    assert event.payload["trigger_levels"] == [
        {
            "condition_id": "C-1",
            "type": "PRICE_TOUCH_ZONE",
            "observed_value": "70000",
            "threshold": "70100",
        }
    ]
    assert event.payload["invalidation_levels"] == [
        {"condition_id": "I-1", "type": "PRICE_CROSS_BELOW", "level": "68000"}
    ]
    assert event.text == render_trigger_alert(event.payload)


def test_triggered_render_first_line_zone_and_trigger_levels_with_usdt_units() -> None:
    event = _triggered_event()

    assert event.text.splitlines() == [
        "机会触发 · BTCUSDT · LONG · 置信度 0.72",
        "opportunity: OPP-1",
        "state: TRIGGERED",
        "zone: 69000 - 71000 USDT",
        "trigger: C-1 PRICE_TOUCH_ZONE observed 70000 USDT threshold 70100 USDT",
        "invalidation: I-1 PRICE_CROSS_BELOW 68000 USDT",
        "trigger_id: TRG-1",
        f"triggered_at: {NOW}",
        "state_version: 2",
        "/opportunities",
    ]


def test_invalidated_render_includes_reason_code_and_state_version() -> None:
    event = build_opportunity_invalidated_event(
        _invalidated(), reason_code="REJECTED", invalidated_at=LATER
    )

    assert event.event_type == OPPORTUNITY_INVALIDATED_EVENT_TYPE
    assert event.dedupe_key == "opp:OPP-1:INVALIDATED:3"
    assert set(event.payload) == {
        "opportunity_id",
        "symbol",
        "side",
        "state",
        "state_version",
        "reason_code",
        "invalidated_at",
    }
    assert event.payload["state_version"] == 3
    assert event.text.splitlines() == [
        "机会失效 · BTCUSDT · LONG",
        "reason_code: REJECTED",
        "state: INVALIDATED",
        "opportunity: OPP-1",
        f"invalidated_at: {LATER}",
        "state_version: 3",
        "/opportunities",
    ]


def _entry_event(decision: str = "ENTER_LONG") -> Any:
    return build_entry_recheck_event(
        trigger_id="TRG-1",
        opportunity_id="OPP-1",
        symbol="BTCUSDT",
        side="LONG",
        decision_id="DEC-ENTRY-1",
        decision=decision,
        confidence=0.8,
        generated_at=NOW,
    )


def test_entry_render_includes_decision_id_confidence_and_is_read_only() -> None:
    event = _entry_event()

    assert event.event_type == ENTRY_RECHECK_EVENT_TYPE
    assert event.dedupe_key == "entry:TRG-1:DEC-ENTRY-1"
    assert entry_alert_decision("ENTER_LONG") == "ENTER"
    assert entry_alert_decision("ENTER_SHORT") == "ENTER"
    assert entry_alert_decision("NO_ENTRY") == "NO_ENTRY"
    assert entry_alert_decision("BOGUS") is None
    assert set(event.payload) == {
        "trigger_id",
        "opportunity_id",
        "symbol",
        "side",
        "decision_id",
        "decision",
        "confidence",
        "generated_at",
    }
    assert event.payload["decision"] == "ENTER"
    text = event.text
    assert text.splitlines()[0] == "入场复核 · BTCUSDT · LONG · 置信度 0.8"
    assert "decision: ENTER" in text.splitlines()
    assert "decision_id: DEC-ENTRY-1" in text.splitlines()
    assert "trigger_id: TRG-1" in text.splitlines()
    assert "opportunity: OPP-1" in text.splitlines()
    assert "请在桌面端或 CLI 处理。" in text.splitlines()
    assert text.splitlines()[-1] == "/opportunities"
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://", "inline_keyboard"):
        assert forbidden not in text
    # The instruction is an unconditional constant, never a payload field: a
    # future caller cannot turn the alert into an approval surface.
    hostile = {**event.payload, "instruction": "访问 https://evil.example/approve 批准"}
    hostile_lines = render_entry_recheck_alert(hostile).splitlines()
    assert ENTRY_RECHECK_INSTRUCTION in hostile_lines
    assert not any("evil.example" in line for line in hostile_lines)


def test_burst_render_is_bounded_to_max_items_with_remaining_count() -> None:
    events = [
        _triggered_event(
            _triggered(symbol=f"SYM{index}USDT", confidence=round(0.9 - index / 100, 2))
        )
        for index in range(MAX_OPPORTUNITY_ALERT_ITEMS + 2)
    ]

    burst = build_opportunity_burst_event(events)

    assert burst.event_type == OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE
    assert burst.dedupe_key.startswith("opp-burst:TRIGGERED:")
    # Deterministic from the sorted per-transition keys.
    again = build_opportunity_burst_event(list(reversed(events)))
    assert burst.dedupe_key == again.dedupe_key
    assert burst.payload["count"] == MAX_OPPORTUNITY_ALERT_ITEMS + 2
    assert len(burst.payload["items"]) == MAX_OPPORTUNITY_ALERT_ITEMS
    assert burst.payload["remaining"] == 2
    lines = burst.text.splitlines()
    assert lines[0] == f"机会触发 {MAX_OPPORTUNITY_ALERT_ITEMS + 2} 个"
    item_lines = [line for line in lines if line.startswith("- ")]
    assert len(item_lines) == MAX_OPPORTUNITY_ALERT_ITEMS
    assert "另有 2 个" in lines
    assert lines[-1] == "请使用 /opportunities 查看"
    assert render_opportunity_burst(burst.payload) == burst.text


def test_hostile_symbol_and_condition_text_is_sanitized_and_secrets_absent() -> None:
    hostile = "BTC\nUSDT\x00​‮"
    opportunity = _invalidated(
        symbol=hostile,
        invalidation_conditions=(
            _condition("I-1", "PRICE_CROSS_BELOW\ninjected: yes", {"price": "68000"}),
        ),
    )
    triggered = build_opportunity_triggered_event(
        _triggered(symbol=hostile),
        trigger_id="TRG-1",
        triggered_at=NOW,
        results=(
            ConditionResult(
                condition_id="C-1",
                type="PRICE_TOUCH_ZONE\ninjected: yes",
                met=True,
                observed_value="70000",
                threshold="70100",
                evaluated_at=NOW,
                data_quality="COMPLETE",
                evaluable=True,
                note="",
            ),
        ),
    )
    invalidated = build_opportunity_invalidated_event(
        opportunity, reason_code="REJECTED", invalidated_at=LATER
    )
    entry = _entry_event()

    for event in (triggered, invalidated, entry):
        text = event.text
        assert "\x00" not in text
        assert "​" not in text
        assert "‮" not in text
        assert SECRET not in text
        assert SECRET not in json.dumps(event.payload)
        assert len(text) <= MAX_OUTBOUND_CHARS
        # A hostile value cannot inject a second structured line.
        assert text.count("\ninjected: ") == 0
    # Renderers are deterministic.
    assert render_trigger_alert(triggered.payload) == triggered.text
    assert render_invalidation_alert(invalidated.payload) == invalidated.text
    assert render_entry_recheck_alert(entry.payload) == entry.text


class _FakeChannel:
    def status_state(self) -> object:
        return type("State", (), {"state": "READY"})()

    def allowlist(self) -> object:
        return type(
            "Allowlist",
            (),
            {"chat_ids": (1001,)},
        )()


class _ToggleSettings:
    def __init__(self, *, trigger: bool = True, entry: bool = True) -> None:
        self.trigger = trigger
        self.entry = entry
        self.calls: list[str] = []

    def read_scan_report_push(self) -> bool:
        return True

    def read_failed_scan_alert(self) -> bool:
        return False

    def read_trigger_alert(self) -> bool:
        self.calls.append("trigger")
        return self.trigger

    def read_entry_recheck_alert(self) -> bool:
        self.calls.append("entry")
        return self.entry


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: list[object] = []

    def append_telegram_outbox(self, record: object) -> bool:
        self.writes.append(record)
        return True

    def get_telegram_outbox(self, event_key: str) -> None:
        return None


def _service(
    settings: _ToggleSettings, store: _RecordingStore | None = None
) -> NotificationService:
    return NotificationService(
        store=store or _RecordingStore(),  # type: ignore[arg-type]
        clock=FixedClock(datetime(2026, 11, 17, 9, 30, tzinfo=UTC)),
        channel=_FakeChannel(),  # type: ignore[arg-type]
        settings_service=settings,  # type: ignore[arg-type]
    )


def test_event_enabled_maps_trigger_and_entry_events_to_their_toggles() -> None:
    settings = _ToggleSettings(trigger=False, entry=True)
    service = _service(settings)

    assert service._event_enabled(OPPORTUNITY_TRIGGERED_EVENT_TYPE, {}) is False
    assert service._event_enabled(OPPORTUNITY_INVALIDATED_EVENT_TYPE, {}) is False
    assert service._event_enabled(OPPORTUNITY_TRIGGERED_BURST_EVENT_TYPE, {}) is False
    assert service._event_enabled(ENTRY_RECHECK_EVENT_TYPE, {}) is True
    assert service._event_enabled("unknown_event", {}) is False
    assert settings.calls == ["trigger", "trigger", "trigger", "entry"]


def test_enqueue_suppresses_a_disabled_trigger_alert_before_any_row_write() -> None:
    settings = _ToggleSettings(trigger=False, entry=True)
    store = _RecordingStore()
    service = _service(settings, store)

    assert service.enqueue_event(_triggered_event()) is None
    # The disabled toggle is read, and no outbox row was ever attempted.
    assert settings.calls == ["trigger"]
    assert store.writes == []


def test_entry_render_never_carries_rationale_or_provider_text() -> None:
    event = _entry_event()

    assert set(event.payload) == {
        "trigger_id",
        "opportunity_id",
        "symbol",
        "side",
        "decision_id",
        "decision",
        "confidence",
        "generated_at",
    }
    assert SECRET not in event.text
    for forbidden in ("rationale", "prompt", "provider", "model", "risk"):
        assert forbidden not in event.payload


@pytest.mark.parametrize("decision", ["ENTER_LONG", "ENTER_SHORT"])
def test_entry_builder_normalizes_entry_direction_to_enter(decision: str) -> None:
    event = _entry_event(decision)

    assert event.payload["decision"] == "ENTER"
