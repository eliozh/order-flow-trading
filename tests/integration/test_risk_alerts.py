"""Integration: risk alerts enqueue once per crossing and period."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from oft.adapters.llm_stub import LLMStub
from oft.application.entry_service import EntryService
from oft.application.freshness import FreshnessGuard
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    DAILY_EQUITY_STATE_KEY,
    ManagementService,
)
from oft.application.notifications import NotificationFreshnessSink
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.cli.main import _build_freshness_guard
from oft.config import Settings
from oft.domain.telegram import RISK_PUSH_KEY
from oft.ports.llm import LLMRole
from oft.ports.store import ConfigRecord

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)
NOW_TEXT = "2026-10-08T00:00:00.000Z"
SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant += delta


def _setup(tmp_path: Path, *, clock: _MutableClock) -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=clock)
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=clock).evaluate(opportunity.opportunity_id)
    assert run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=clock)
    entry = EntryService(
        store=container.store,
        clock=clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="risk-alert-entry")
    assert accepted.position is not None
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.MANAGEMENT: "management_add.json"},
    )
    service = ManagementService(
        store=container.store,
        clock=clock,
        provider=provider,
        settings=container.settings,
        kill_switch=kill,
        runtime=container.llm_runtime,
        notifications=container.notifications,
    )
    return {
        "container": container,
        "settings": settings,
        "service": service,
        "position": accepted.position,
    }


def _set_over_leverage(settings: Settings, position_id: str) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["leverage"] = 50
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def _risk_receipts(container: Any) -> list[Any]:
    return [
        receipt
        for receipt in container.store.list_telegram_receipts()
        if receipt.kind == "risk_alert"
    ]


def _text(receipt: Any) -> str:
    return json.loads(receipt.payload_json)["text"]


def test_a_leverage_cap_block_alerts_once_per_day(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    _set_over_leverage(setup["settings"], position_id)

    first = setup["service"].manage(position_id, idempotency_key="risk-add-1")
    assert first.status == "BLOCKED"
    assert first.risk is not None
    assert first.risk["binding_cap"] == "MAX_LEVERAGE"

    receipts = _risk_receipts(container)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == ("risk:MAX_LEVERAGE:BTCUSDT:2026-10-08:BLOCKED")
    text = _text(receipt)
    assert text.splitlines()[0] == "风险告警 · MAX_LEVERAGE · BLOCKED"
    assert "kind: MAX_LEVERAGE" in text.splitlines()
    assert "observed: 50 x" in text.splitlines()
    assert "binding_cap: MAX_LEVERAGE" in text.splitlines()
    assert "period: 2026-10-08" in text.splitlines()
    assert SENTINEL not in text

    # A second blocked tick in the same day is a durable no-op.
    second = setup["service"].manage(position_id, idempotency_key="risk-add-2")
    assert second.status == "BLOCKED"
    assert len(_risk_receipts(container)) == 1


def test_a_toggled_off_risk_push_suppresses_the_alert(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]
    position_id = setup["position"]["position_id"]
    _set_over_leverage(setup["settings"], position_id)
    container.store.set_config(
        ConfigRecord(
            key=RISK_PUSH_KEY,
            value_json="false",
            updated_at=NOW_TEXT,
            updated_by="test",
        )
    )

    result = setup["service"].manage(position_id, idempotency_key="risk-toggled-off")

    assert result.status == "BLOCKED"
    assert _risk_receipts(container) == []


def test_a_stale_stream_alerts_degraded_and_recovered_once_per_episode(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]
    sink = NotificationFreshnessSink(container.notifications)
    guard = FreshnessGuard(clock=clock, alert_sink=sink)
    guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))

    guard.evaluate(now=NOW)
    guard.evaluate(now=NOW)

    receipts = [r for r in container.store.list_telegram_receipts() if r.kind == "degradation"]
    assert len(receipts) == 1
    assert "系统降级 · WS:BTCUSDT" in _text(receipts[0])
    assert "reason_code: MARKET_DATA_STALE" in _text(receipts[0])

    guard.record("WS:BTCUSDT", NOW)
    guard.evaluate(now=NOW + timedelta(seconds=1))
    guard.evaluate(now=NOW + timedelta(seconds=2))

    receipts = [r for r in container.store.list_telegram_receipts() if r.kind == "degradation"]
    assert len(receipts) == 2
    texts = [_text(receipt) for receipt in receipts]
    assert any("系统恢复 · WS:BTCUSDT" in text for text in texts)
    assert any("state: RECOVERED" in text for text in texts)


def _set_day_start(container: Any, *, day: str, equity: Any) -> None:
    container.store.set_system_state(
        DAILY_EQUITY_STATE_KEY,
        json.dumps({"day": day, "equity": str(equity)}),
        updated_at=NOW_TEXT,
    )


def test_daily_loss_approach_then_breach_alerts_once_per_state(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]
    service = setup["service"]
    position_id = setup["position"]["position_id"]

    measured = service.risk_context(now=NOW_TEXT)
    assert measured.measured and measured.daily_loss is not None
    equity = measured.equity
    limit = equity * Decimal("0.03")

    _set_day_start(container, day="2026-10-08", equity=equity + limit * Decimal("0.85"))
    service.due_conditions(
        position_id,
        window_start=NOW_TEXT,
        window_end="2026-10-08T00:05:00.000Z",
        now=NOW_TEXT,
    )
    approach = _risk_receipts(container)
    assert len(approach) == 1
    assert approach[0].event_key == "risk:DAILY_LOSS:ACCOUNT:2026-10-08:APPROACH"

    _set_day_start(container, day="2026-10-08", equity=equity + limit * Decimal("1.1"))
    service.due_conditions(
        position_id,
        window_start=NOW_TEXT,
        window_end="2026-10-08T00:05:00.000Z",
        now=NOW_TEXT,
    )
    receipts = _risk_receipts(container)
    assert sorted(receipt.event_key for receipt in receipts) == [
        "risk:DAILY_LOSS:ACCOUNT:2026-10-08:APPROACH",
        "risk:DAILY_LOSS:ACCOUNT:2026-10-08:BREACH",
    ]
    breach = next(receipt for receipt in receipts if receipt.event_key.endswith(":BREACH"))
    assert "observed: " in _text(breach) and "observed: N/A" not in _text(breach)

    # A repeated breach tick in the same period is a durable no-op.
    service.due_conditions(
        position_id,
        window_start=NOW_TEXT,
        window_end="2026-10-08T00:05:00.000Z",
        now=NOW_TEXT,
    )
    assert len(_risk_receipts(container)) == 2


def test_a_clamped_add_alerts_the_notional_cap_once(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]
    service = setup["service"]
    position_id = setup["position"]["position_id"]

    first = service.manage(position_id, idempotency_key="risk-notional-1")
    assert first.status == "EXECUTED"
    assert first.risk is not None
    assert first.risk["binding_cap"] == "MAX_NOTIONAL"

    receipts = _risk_receipts(container)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.event_key == "risk:MAX_NOTIONAL:BTCUSDT:2026-10-08:REDUCED"
    text = _text(receipt)
    assert "kind: MAX_NOTIONAL" in text.splitlines()
    assert "state: REDUCED" in text.splitlines()
    assert "observed: N/A" not in text
    assert "threshold: N/A" not in text

    service.manage(position_id, idempotency_key="risk-notional-2")
    assert len(_risk_receipts(container)) == 1


def _entry_alert_setup(tmp_path: Path, *, clock: _MutableClock) -> tuple[Any, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
    container = build_container(settings, initialize=True, clock=clock)
    assert (
        container.backfill.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        ).run.status
        == "COMPLETE"
    )
    ScanService(container=container, lock=container.scan_lock).run(
        ScanRequest(symbol="BTCUSDT", period="4h", metrics_only=False, provider="stub")
    )
    opportunity = container.store.list_opportunities(state="OPEN")[0]
    run = TriggerService(store=container.store, clock=clock).evaluate(opportunity.opportunity_id)
    assert run.trigger is not None
    container.telegram_service.set_token(SENTINEL, actor="test")
    container.telegram_service.update_allowlist([1001], [2002], actor="test")
    provider = LLMStub(
        fixtures_dir=FIXTURES,
        overrides={LLMRole.ENTRY_RECHECK: "entry_recheck_oversized.json"},
    )
    entry = EntryService(
        store=container.store,
        clock=clock,
        provider=provider,
        settings=container.settings,
        kill_switch=KillSwitch(settings=container.settings, store=container.store, clock=clock),
        notifications=container.notifications,
    )
    return container, entry.accept(run.trigger.trigger_id, idempotency_key="risk-entry-notional")


def test_the_entry_path_alerts_a_notional_clamp(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    container, accepted = _entry_alert_setup(tmp_path, clock=clock)

    assert accepted.risk_verdict["verdict"] == "REDUCE"
    assert accepted.risk_verdict["binding_cap"] == "MAX_NOTIONAL"

    receipts = _risk_receipts(container)
    assert len(receipts) == 1
    assert receipts[0].event_key == "risk:MAX_NOTIONAL:BTCUSDT:2026-10-08:REDUCED"
    text = _text(receipts[0])
    assert "state: REDUCED" in text.splitlines()
    assert "observed: N/A" not in text
    assert SENTINEL not in text


def test_the_container_rate_limiter_enqueues_the_degradation(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]

    container.rate_limiter.note_status(429, endpoint="/fapi/v1/aggTrades")

    receipts = [r for r in container.store.list_telegram_receipts() if r.kind == "degradation"]
    assert len(receipts) == 1
    text = _text(receipts[0])
    assert "reason_code: RATE_LIMITED_429" in text.splitlines()
    assert "系统降级 · provider:binance" in text.splitlines()


def test_the_daemon_freshness_guard_composes_the_degradation_sink(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    setup = _setup(tmp_path, clock=clock)
    container = setup["container"]

    guard = _build_freshness_guard(container)
    guard.record("WS:BTCUSDT", NOW - timedelta(seconds=120))
    guard.evaluate(now=NOW)

    receipts = [r for r in container.store.list_telegram_receipts() if r.kind == "degradation"]
    assert len(receipts) == 1
    text = _text(receipts[0])
    assert "系统降级 · WS:BTCUSDT" in text
    assert "reason_code: MARKET_DATA_STALE" in text.splitlines()
