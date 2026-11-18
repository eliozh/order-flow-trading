"""Unit: budget-threshold event builder, renderer, and cooldown identity."""

from __future__ import annotations

from typing import Any

from oft.application.telegram_events import (
    BUDGET_ALERT_PRIORITY,
    BUDGET_SUBJECT_KIND,
    BUDGET_THRESHOLD_EVENT_TYPE,
    NotificationEvent,
    budget_alert_cooldown_key,
    build_budget_threshold_event,
    enqueue_budget_threshold,
)
from oft.application.telegram_messages import (
    BUDGET_CONTINUES_NOTICE,
    render_budget_alert,
)

NOW_TEXT = "2026-11-17T09:30:00.000Z"


def _event(**overrides: Any) -> NotificationEvent:
    values: dict[str, Any] = {
        "period": "day",
        "period_key": "2026-11-17",
        "spend_usd": "8.25",
        "threshold_usd": "10.00",
        "state": "WARN",
        "unpriced_count": 2,
        "occurred_at": NOW_TEXT,
    }
    values.update(overrides)
    return build_budget_threshold_event(**values)


def test_renderer_shows_spend_threshold_percentage_and_the_continues_notice() -> None:
    event = _event()

    lines = event.text.splitlines()

    assert lines[0] == "预算告警 · DAY · WARN"
    assert "period: day" in lines
    assert "period_key: 2026-11-17" in lines
    assert "spend_usd: 8.25 USDT" in lines
    assert "threshold_usd: 10 USDT" in lines
    assert "percentage: 82.5 %" in lines
    assert "state: WARN" in lines
    assert "unpriced_count: 2" in lines
    assert f"occurred_at: {NOW_TEXT}" in lines
    assert BUDGET_CONTINUES_NOTICE in lines
    assert BUDGET_CONTINUES_NOTICE == "达到 80% 与 100% 阈值时经 Telegram 告警；不会停止调用。"


def test_percentage_is_na_when_the_threshold_or_spend_is_unmeasurable() -> None:
    assert (
        render_budget_alert(_event(threshold_usd="0", spend_usd="1.00").payload).count(
            "percentage: N/A"
        )
        == 1
    )
    assert _event(threshold_usd="0").payload["percentage"] is None
    assert _event(threshold_usd="not-a-number").payload["percentage"] is None
    assert _event(spend_usd="not-a-number").payload["percentage"] is None
    assert "percentage: N/A" in render_budget_alert(_event(spend_usd="x").payload).splitlines()


def test_an_absurd_exponent_renders_na_and_never_raises() -> None:
    event = _event(spend_usd="1e999999999")

    assert event.payload["percentage"] is None
    assert "percentage: N/A" in render_budget_alert(event.payload).splitlines()


def test_unpriced_count_none_is_na_but_zero_is_rendered_honestly() -> None:
    missing = _event(unpriced_count=None).text.splitlines()
    zero = _event(unpriced_count=0).text.splitlines()

    assert "unpriced_count: N/A" in missing
    assert "unpriced_count: 0" in zero


def test_payload_is_an_allowlist_and_hostile_text_cannot_inject_lines() -> None:
    event = _event(period_key="2026-11-17\nmodel: gpt-leak\x00")

    assert set(event.payload) == {
        "period",
        "period_key",
        "spend_usd",
        "threshold_usd",
        "percentage",
        "state",
        "unpriced_count",
        "occurred_at",
    }
    assert "provider" not in event.payload
    assert "model" not in event.payload
    # Every rendered line stays whole: the hostile newline became a space.
    assert "period_key: 2026-11-17 model: gpt-leak" in event.text.splitlines()
    assert "\nmodel:" not in event.text


def test_event_identity_matches_the_slice_contract() -> None:
    event = _event()

    assert event.event_type == BUDGET_THRESHOLD_EVENT_TYPE == "budget_threshold"
    assert event.priority == BUDGET_ALERT_PRIORITY
    assert event.subject["kind"] == BUDGET_SUBJECT_KIND == "BUDGET"


def test_dedupe_key_includes_period_state_and_period_key() -> None:
    base = _event()

    assert base.dedupe_key == "budget:day:WARN:2026-11-17"
    assert _event(period_key="2026-11-18").dedupe_key != base.dedupe_key
    assert _event(period="week").dedupe_key != base.dedupe_key
    assert _event(state="EXCEEDED").dedupe_key != base.dedupe_key


def test_cooldown_key_is_stable_across_period_keys() -> None:
    # The alerted period is the stored marker value, never part of the key, so
    # system_state rows stay bounded by the alert vocabulary.
    assert budget_alert_cooldown_key("day", "WARN") == "budget:day:WARN"
    assert budget_alert_cooldown_key("day", "EXCEEDED") == "budget:day:EXCEEDED"
    assert budget_alert_cooldown_key("week", "WARN") == "budget:week:WARN"


class _Sink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def enqueue_event(self, event: NotificationEvent) -> object | None:
        self.calls.append({"event": event})
        return event

    def enqueue_cooled(
        self, event: NotificationEvent, *, cooldown_key: str, period: str
    ) -> object | None:
        self.calls.append({"event": event, "cooldown_key": cooldown_key, "period": period})
        return event


def _enqueue(sink: _Sink, **overrides: Any) -> object | None:
    values: dict[str, Any] = {
        "period": "day",
        "period_key": "2026-11-17",
        "spend_usd": "8.25",
        "threshold_usd": "10.00",
        "state": "WARN",
        "unpriced_count": 2,
        "occurred_at": NOW_TEXT,
    }
    values.update(overrides)
    return enqueue_budget_threshold(sink, **values)


def test_enqueue_uses_the_stable_cooldown_and_stores_the_period_value() -> None:
    sink = _Sink()

    record = _enqueue(sink)

    assert record is not None
    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["cooldown_key"] == "budget:day:WARN"
    assert call["period"] == "2026-11-17"
    assert call["event"].dedupe_key == "budget:day:WARN:2026-11-17"


def test_enqueue_rejects_unknown_periods_and_non_alert_states() -> None:
    sink = _Sink()

    assert _enqueue(sink, period="year") is None
    assert _enqueue(sink, state="OK") is None
    assert sink.calls == []
