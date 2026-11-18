"""Position/risk/degradation push events: allowlisted payloads and renderers."""

from __future__ import annotations

import json
from decimal import Decimal

from oft.application.telegram_events import (
    DEGRADATION_EVENT_TYPE,
    POSITION_MANAGEMENT_EVENT_TYPE,
    RISK_ALERT_EVENT_TYPE,
    build_degradation_event,
    build_position_management_event,
    build_risk_alert_event,
    daily_loss_alert_state,
    risk_alert_kind_for_cap,
)
from oft.application.telegram_messages import (
    READ_ONLY_NOTICE,
    render_degradation_alert,
    render_position_management_alert,
    render_risk_alert,
)
from oft.domain.telegram import MAX_OUTBOUND_CHARS

NOW = "2026-11-17T09:30:00.000Z"
SECRET = "SECRET_PROMPT_TEXT provider-raw-body sk-abcdef0123456789"


def _position_event(**overrides: object) -> object:
    values: dict[str, object] = {
        "position_id": "POS-1",
        "symbol": "BTCUSDT",
        "action": "PARTIAL_CLOSE",
        "status": "EXECUTED",
        "size": "0.01",
        "price": "70000",
        "reason_code": "PARTIAL_CLOSE",
        "decision_id": "MGD-1",
        "occurred_at": NOW,
    }
    values.update(overrides)
    return build_position_management_event(**values)  # type: ignore[arg-type]


def test_position_event_is_allowlisted_and_read_only() -> None:
    event = _position_event()

    assert event.event_type == POSITION_MANAGEMENT_EVENT_TYPE
    assert event.dedupe_key == "position:MGD-1:EXECUTED:PARTIAL_CLOSE"
    assert event.subject == {"kind": "POSITION", "id": "POS-1"}
    assert set(event.payload) == {
        "position_id",
        "symbol",
        "action",
        "status",
        "size",
        "price",
        "reason_code",
        "decision_id",
        "occurred_at",
    }
    # No order/approval field may imply a Telegram action.
    for forbidden in ("order", "approval", "action_url", "callback", "inline_keyboard"):
        assert forbidden not in event.payload
    lines = event.text.splitlines()
    assert lines[0] == "持仓管理 · BTCUSDT · PARTIAL_CLOSE"
    assert "position: POS-1" in lines
    assert "action: PARTIAL_CLOSE" in lines
    assert "status: EXECUTED" in lines
    assert "size: 0.01" in lines
    assert "price: 70000 USDT" in lines
    assert "reason_code: PARTIAL_CLOSE" in lines
    assert "decision_id: MGD-1" in lines
    assert READ_ONLY_NOTICE in lines
    for forbidden in ("/approve", "/accept", "/reject", "http://", "https://"):
        assert forbidden not in event.text
    assert len(event.text) <= MAX_OUTBOUND_CHARS


def test_blocked_position_event_reports_reason_code_without_size_or_price() -> None:
    event = _position_event(status="BLOCKED", action="ADD", size=None, price=None)

    assert event.dedupe_key == "position:MGD-1:BLOCKED:ADD"
    lines = event.text.splitlines()
    assert lines[0] == "持仓管理 · BTCUSDT · ADD"
    assert "status: BLOCKED" in lines
    assert "size: N/A" in lines
    assert "price: N/A" in lines


def test_risk_event_carries_kind_state_and_units() -> None:
    event = build_risk_alert_event(
        kind="MAX_LEVERAGE",
        subject="BTCUSDT",
        binding_cap="MAX_LEVERAGE",
        state="BLOCKED",
        observed="50",
        threshold="5",
        unit="x",
        period="2026-11-17",
        reason_code="RISK_BLOCKED",
        occurred_at=NOW,
    )

    assert event.event_type == RISK_ALERT_EVENT_TYPE
    assert event.dedupe_key == "risk:MAX_LEVERAGE:BTCUSDT:2026-11-17:BLOCKED"
    assert event.subject == {"kind": "RISK", "id": "MAX_LEVERAGE:BTCUSDT"}
    lines = event.text.splitlines()
    assert lines[0] == "风险告警 · MAX_LEVERAGE · BLOCKED"
    assert "kind: MAX_LEVERAGE" in lines
    assert "state: BLOCKED" in lines
    assert "subject: BTCUSDT" in lines
    assert "binding_cap: MAX_LEVERAGE" in lines
    assert "observed: 50 x" in lines
    assert "threshold: 5 x" in lines
    assert "period: 2026-11-17" in lines
    assert READ_ONLY_NOTICE in lines


def test_risk_event_renders_daily_loss_in_usdt() -> None:
    event = build_risk_alert_event(
        kind="DAILY_LOSS",
        subject="ACCOUNT",
        binding_cap="DAILY_LOSS",
        state="APPROACH",
        observed="65.00",
        threshold="80.00",
        unit="USDT",
        period="2026-11-17",
        reason_code="DAILY_LOSS_APPROACH",
        occurred_at=NOW,
    )

    assert "observed: 65 USDT" in event.text.splitlines()
    assert "threshold: 80 USDT" in event.text.splitlines()
    assert event.text.splitlines()[0] == "风险告警 · DAILY_LOSS · APPROACH"


def test_degradation_event_one_per_state_with_recovery_copy() -> None:
    degraded = build_degradation_event(
        subject="WS:BTCUSDT",
        state="DEGRADED",
        reason_code="MARKET_DATA_STALE",
        occurred_at=NOW,
        episode=NOW,
    )
    recovered = build_degradation_event(
        subject="WS:BTCUSDT",
        state="RECOVERED",
        reason_code="MARKET_DATA_RECOVERED",
        occurred_at=NOW,
        episode=NOW,
    )

    assert degraded.event_type == DEGRADATION_EVENT_TYPE
    assert degraded.dedupe_key == f"degraded:WS:BTCUSDT:DEGRADED:{NOW}"
    assert degraded.text.splitlines()[0] == "系统降级 · WS:BTCUSDT"
    assert "state: DEGRADED" in degraded.text.splitlines()
    assert recovered.text.splitlines()[0] == "系统恢复 · WS:BTCUSDT"
    assert "state: RECOVERED" in recovered.text.splitlines()
    assert recovered.dedupe_key != degraded.dedupe_key


def test_risk_cap_mapping_and_daily_loss_threshold_states() -> None:
    assert risk_alert_kind_for_cap("DAILY_LOSS") == "DAILY_LOSS"
    assert risk_alert_kind_for_cap("MAX_LEVERAGE") == "MAX_LEVERAGE"
    assert risk_alert_kind_for_cap("MAX_NOTIONAL") == "MAX_NOTIONAL"
    assert risk_alert_kind_for_cap("STALE_MARK") is None

    assert daily_loss_alert_state(daily_loss=Decimal("0"), limit=Decimal("80")) is None
    assert daily_loss_alert_state(daily_loss=Decimal("64"), limit=Decimal("80")) == "APPROACH"
    assert daily_loss_alert_state(daily_loss=Decimal("80"), limit=Decimal("80")) == "BREACH"
    assert daily_loss_alert_state(daily_loss=Decimal("120"), limit=Decimal("80")) == "BREACH"
    assert daily_loss_alert_state(daily_loss=None, limit=Decimal("80")) is None


def test_hostile_text_is_sanitized_and_secrets_absent() -> None:
    hostile = "BTC\nUSDT\x00​‮"
    event = _position_event(symbol=hostile, reason_code="BAD\nreason")
    risk = build_risk_alert_event(
        kind="DAILY_LOSS",
        subject=hostile,
        binding_cap="DAILY_LOSS",
        state="BREACH",
        observed="1",
        threshold="1",
        unit="USDT",
        period="2026-11-17",
        reason_code="BAD\nreason",
        occurred_at=NOW,
    )
    degraded = build_degradation_event(
        subject=hostile,
        state="DEGRADED",
        reason_code="BAD\nreason",
        occurred_at=NOW,
        episode=NOW,
    )

    for item in (event, risk, degraded):
        text = item.text
        assert "\x00" not in text
        assert "​" not in text
        assert "‮" not in text
        assert SECRET not in text
        assert SECRET not in json.dumps(item.payload)
        assert text.count("\nreason: ") == 0
        assert len(text) <= MAX_OUTBOUND_CHARS
    assert render_position_management_alert(event.payload) == event.text
    assert render_risk_alert(risk.payload) == risk.text
    assert render_degradation_alert(degraded.payload) == degraded.text
