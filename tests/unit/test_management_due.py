"""Management due/condition evaluation: CADENCE, FUNDING_SETTLEMENT, RISK_BREACH.

These are unit tests: a real SQLite store holds one OPEN position payload, but no
LLM provider is ever constructed. The deterministic loss helpers are exercised
directly so the breach arithmetic is pinned without a market or a clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import oft.application.management_service as management_service_module
from oft.adapters.sqlite_store import SqliteStore
from oft.application.entry_service import caps_from_settings
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    CADENCE_CONDITION,
    DAILY_EQUITY_STATE_KEY,
    FUNDING_SETTLEMENT_CONDITION,
    RISK_BREACH_CONDITION,
    ManagementRiskContext,
    ManagementService,
    daily_loss_breach,
    realized_net_for_day,
    unrealized_net_for_position,
)
from oft.application.paper_account import PaperAccountService
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import ReportRecord, ScanRecord, TradeLogRecord

NOW = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)
WINDOW_START = "2026-10-08T00:00:00.000Z"
WINDOW_END = "2026-10-08T04:00:00.000Z"
NOW_TEXT = utc_timestamp(NOW)
POSITION_ID = "POS-DUE-1"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _store(settings: Settings) -> SqliteStore:
    store = SqliteStore(settings)
    store.initialize()
    PaperAccountService(settings=settings, store=store, clock=FixedClock(NOW)).ensure()
    return store


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "position_id": POSITION_ID,
        "account_id": "ACC-PAPER",
        "environment": "PAPER",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "state": "OPEN",
        "size": "0.01",
        "entry_price": "70000.00",
        "avg_entry_price": "70000.00",
        "mark_price": "70000.00",
        "notional_usd": "700.00",
        "stop_price": "69000.00",
        "target_prices": [],
        "leverage": 2.0,
        "margin_usd": "350.00",
        "risk_amount_usd": "10.00",
        "risk_pct": 0.1,
        "fees_usd": "0",
        "funding_usd": "0",
        "opened_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
        "source_trigger_id": None,
        "source_decision_id": None,
        "closed_at": None,
        "exit_price": None,
        "realized_pnl_usd": "0",
        "management_history": [],
        "version": 1,
    }
    payload.update(overrides)
    return payload


def _insert_position(settings: Settings, payload: dict[str, object]) -> None:
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO positions "
            "(position_id, account_id, environment, symbol, state, opened_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(payload["position_id"]),
                str(payload["account_id"]),
                str(payload["environment"]),
                str(payload["symbol"]),
                str(payload["state"]),
                str(payload["opened_at"]),
                json.dumps(payload, sort_keys=True),
            ),
        )
        connection.commit()


def _insert_trade(
    settings: Settings, trade_id: str, position_id: str, closed_at: str, net: str
) -> None:
    """Insert one recorded closed trade (the risk measure's realized input).

    The test connection leaves SQLite's foreign-key pragma at its default
    ``OFF``, so the journal's parent rows are not needed for this arithmetic.
    """
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_logs "
            "(trade_id, position_id, account_id, environment, symbol, side, opened_at, "
            "closed_at, net_pnl_usd, idempotency_key, payload_json, order_id, fill_id, "
            "risk_verdict_id, decision_id) "
            "VALUES (?, ?, 'ACC-PAPER', 'PAPER', 'BTCUSDT', 'LONG', ?, ?, ?, ?, ?, "
            "'ORD', 'FILL', 'RISK', 'DEC')",
            (
                trade_id,
                position_id,
                "2026-10-07T00:00:00.000Z",
                closed_at,
                net,
                f"IDEM-{trade_id}",
                json.dumps({"net_pnl_usd": net}),
            ),
        )
        connection.commit()


def _seed_day_start(store: SqliteStore, day: date, equity: str) -> None:
    """Persist the UTC-day-start equity snapshot the loss is measured against."""
    store.set_system_state(
        DAILY_EQUITY_STATE_KEY,
        json.dumps({"day": day.isoformat(), "equity": equity}),
        updated_at=NOW_TEXT,
    )


def _service(
    settings: Settings,
    store: SqliteStore,
    *,
    provider: object | None = None,
    funding_enabled: bool = True,
    risk_enabled: bool = True,
) -> ManagementService:
    return ManagementService(
        store=store,
        clock=FixedClock(NOW),
        provider=provider,  # type: ignore[arg-type]
        settings=settings,
        kill_switch=KillSwitch(settings=settings, store=store, clock=FixedClock(NOW)),
        funding_settlement_enabled=funding_enabled,
        risk_breach_enabled=risk_enabled,
    )


def _due(service: ManagementService, position_id: str = POSITION_ID):  # type: ignore[no-untyped-def]
    return service.due_conditions(
        position_id,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW_TEXT,
    )


def test_cadence_is_due_at_a_boundary_for_an_open_position(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    service = _service(settings, store)

    due = _due(service)

    assert due.due is True
    assert due.conditions == (CADENCE_CONDITION,)
    assert CADENCE_CONDITION in due.reason


def test_a_missing_position_is_not_due_and_makes_no_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    service = _service(settings, store)

    due = _due(service, "POS-MISSING")

    assert due.due is False
    assert due.conditions == ()
    assert "does not exist" in due.reason


def test_a_non_open_position_is_not_due(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload(state="CLOSED", closed_at=NOW_TEXT))
    service = _service(settings, store)

    due = _due(service)

    assert due.due is False
    assert "not OPEN" in due.reason


def test_funding_settlement_is_reported_only_inside_the_window_and_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    service = _service(settings, store)
    calls: list[tuple[str, str, str]] = []

    def _funding(position_id: str, start: str, end: str) -> str | None:
        calls.append((position_id, start, end))
        return "2026-10-08T02:00:00.000Z"

    monkeypatch.setattr(service._positions, "funding_settlement_in_window", _funding)

    due = _due(service)

    assert calls == [(POSITION_ID, WINDOW_START, WINDOW_END)]
    assert due.conditions == (CADENCE_CONDITION, FUNDING_SETTLEMENT_CONDITION)

    disabled = _service(settings, store, funding_enabled=False)
    monkeypatch.setattr(disabled._positions, "funding_settlement_in_window", _funding)
    assert FUNDING_SETTLEMENT_CONDITION not in _due(disabled).conditions


def test_funding_settlement_without_a_recorded_point_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    service = _service(settings, store)
    monkeypatch.setattr(
        service._positions, "funding_settlement_in_window", lambda *args, **kwargs: None
    )

    assert FUNDING_SETTLEMENT_CONDITION not in _due(service).conditions


def test_risk_breach_is_reported_only_when_the_toggle_is_enabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)

    assert RISK_BREACH_CONDITION in _due(service).conditions

    disabled = _service(settings, store, risk_enabled=False)
    assert RISK_BREACH_CONDITION not in _due(disabled).conditions


def test_risk_breach_is_not_reported_when_the_loss_is_not_measured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    stale_at = utc_timestamp(NOW - timedelta(seconds=120))
    # The real contract carries the stored mark's observation time in
    # ``updated_at``; no synthetic ``mark_as_of`` field exists.
    _insert_position(settings, _payload(updated_at=stale_at))
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)

    assert service.risk_context(now=NOW_TEXT).measured is False
    assert RISK_BREACH_CONDITION not in _due(service).conditions


def test_an_explicit_unmeasured_context_omits_risk_breach_without_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    # A fresh measurement would breach, so the omission is the context's doing.
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)
    real = store.list_trade_logs
    calls = {"count": 0}

    def _counting(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "list_trade_logs", _counting)
    context = ManagementRiskContext.unmeasured()

    due = service.due_conditions(
        POSITION_ID,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW_TEXT,
        risk_context=context,
    )

    assert context.measured is False
    assert context.daily_loss is None
    assert calls["count"] == 0
    assert due.due is True
    assert due.conditions == (CADENCE_CONDITION,)


def test_risk_breach_fires_when_the_measured_loss_exceeds_the_cap(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    # Cash 10,000 + the marked 0.01 @ 70,000 = 10,700 equity; the cap is 3%.
    assert context.equity == Decimal("10700.00000000")
    assert context.daily_loss == Decimal("400.00")
    assert RISK_BREACH_CONDITION in _due(service).conditions


def test_a_fresh_mark_with_a_recorded_losing_trade_breaches(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload(mark_price="69999.00"))
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is True
    # The day's first observation seeds the baseline from current equity, so the
    # already-present intraday mark move is excluded here; the realized loss
    # alone still breaches.
    assert context.daily_loss == Decimal("400.00")
    assert RISK_BREACH_CONDITION in _due(service).conditions


def test_a_small_measured_loss_does_not_breach(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-100.00")
    service = _service(settings, store)

    assert service.risk_context(now=NOW_TEXT).daily_loss == Decimal("100.00")
    assert RISK_BREACH_CONDITION not in _due(service).conditions


def test_two_underwater_positions_breach_the_cap_together(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    # Each position loses 200 (0.01 * (50,000 - 70,000)), below the ~321 cap in
    # the one-position world (day start 10,700); together they lose 400, above
    # the ~342 cap in the two-position world (day start 11,400).
    _insert_position(
        settings,
        _payload(
            position_id="POS-A",
            mark_price="50000.00",
        ),
    )
    _seed_day_start(store, NOW.date(), "10700")
    service = _service(settings, store)
    one_position = service.risk_context(now=NOW_TEXT)
    assert one_position.measured is True
    assert one_position.daily_loss == Decimal("200.0000")
    assert RISK_BREACH_CONDITION not in _due(service, "POS-A").conditions

    _insert_position(
        settings,
        _payload(
            position_id="POS-B",
            mark_price="50000.00",
        ),
    )
    _seed_day_start(store, NOW.date(), "11400")
    context = service.risk_context(now=NOW_TEXT)

    assert context.unrealized_net == Decimal("-400.0000")
    assert context.daily_loss == Decimal("400.0000")
    assert RISK_BREACH_CONDITION in _due(service, "POS-B").conditions


def test_a_net_positive_day_does_not_breach_despite_a_losing_fill(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-WIN", "POS-CLOSED-1", NOW_TEXT, "500.00")
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-2", NOW_TEXT, "-100.00")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.realized_net == Decimal("400.00")
    assert context.daily_loss == Decimal("0")
    assert RISK_BREACH_CONDITION not in _due(service).conditions


def test_daily_loss_breach_reaches_the_configured_pct() -> None:
    caps = caps_from_settings(None)
    assert caps.daily_loss_pct == 3.0
    # ``>=`` matches the execution gate: a loss exactly at the cap blocks.
    assert daily_loss_breach(equity=Decimal("10000"), loss=Decimal("300"), caps=caps) is True
    assert daily_loss_breach(equity=Decimal("10000"), loss=Decimal("299.99"), caps=caps) is False
    assert daily_loss_breach(equity=Decimal("0"), loss=Decimal("1"), caps=caps) is False


def test_realized_net_for_day_sums_gains_and_losses_of_the_current_utc_day() -> None:
    trades = (
        _trade("T-1", "2026-10-08T00:10:00.000Z", "-100.00"),
        _trade("T-2", "2026-10-08T02:00:00.000Z", "50.00"),
        _trade("T-3", "2026-10-07T23:00:00.000Z", "-400.00"),
        _trade("T-4", "2026-10-08T03:00:00.000Z", "-25.00"),
    )

    net = realized_net_for_day(trades, date(2026, 10, 8))

    assert net == Decimal("-75.00")


def test_unrealized_net_is_side_aware_and_signed() -> None:
    assert unrealized_net_for_position(
        "LONG", Decimal("70000"), Decimal("0.01"), Decimal("69000")
    ) == Decimal("-10.00000")
    assert unrealized_net_for_position(
        "SHORT", Decimal("70000"), Decimal("0.01"), Decimal("71000")
    ) == Decimal("-10.00000")
    # A profitable position is a positive unrealized gain, not zero.
    assert unrealized_net_for_position(
        "LONG", Decimal("70000"), Decimal("0.01"), Decimal("72000")
    ) == Decimal("20.00000")
    with pytest.raises(ValueError):
        unrealized_net_for_position("SIDEWAYS", Decimal("70000"), Decimal("0.01"), Decimal("72000"))


def test_configured_reflects_whether_a_provider_was_injected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)

    class _Provider:
        provider = "stub"

    assert _service(settings, store).configured is False
    assert _service(settings, store, provider=_Provider()).configured is True


class _LogRecorder:
    """Capture the module's structured warnings without depending on config."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def test_unmeasured_risk_names_its_reason_in_the_due_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M13: a degraded measurement is visible, not a silent no-breach."""
    settings = _settings(tmp_path)
    store = _store(settings)
    stale_at = utc_timestamp(NOW - timedelta(seconds=120))
    _insert_position(settings, _payload(updated_at=stale_at))
    service = _service(settings, store)
    recorder = _LogRecorder()
    monkeypatch.setattr(management_service_module, "_LOGGER", recorder)

    due = _due(service)

    assert due.due is True
    assert RISK_BREACH_CONDITION not in due.conditions
    assert "risk_unmeasured" in due.reason
    assert "stale recorded mark" in due.reason
    assert any(event == "management_risk_unmeasured" for event, _ in recorder.events)


def test_a_corrupt_trade_row_makes_the_realized_measure_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M14: a corrupt row must not silently understate the day's realized loss."""
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-BAD", "POS-CLOSED-1", "not-a-timestamp", "-400.00")
    service = _service(settings, store)
    recorder = _LogRecorder()
    monkeypatch.setattr(management_service_module, "_LOGGER", recorder)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is False
    assert context.daily_loss is None
    assert context.degraded_reason is not None
    assert "TRADE-BAD" in context.degraded_reason
    assert any(
        event == "management_risk_unmeasured" and "TRADE-BAD" in kwargs.get("reason", "")
        for event, kwargs in recorder.events
    )


def test_a_non_finite_net_pnl_row_makes_the_realized_measure_unmeasured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _insert_trade(settings, "TRADE-NAN", "POS-CLOSED-1", NOW_TEXT, "NaN")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is False
    assert context.degraded_reason is not None and "TRADE-NAN" in context.degraded_reason


def _trade(trade_id: str, closed_at: str, net: str) -> TradeLogRecord:
    return TradeLogRecord(
        trade_id=trade_id,
        position_id=f"POS-{trade_id}",
        account_id="ACC-PAPER",
        environment="PAPER",
        symbol="BTCUSDT",
        side="LONG",
        opened_at="2026-10-08T00:00:00.000Z",
        closed_at=closed_at,
        net_pnl_usd=net,
        idempotency_key=f"IDEM-{trade_id}",
        payload_json=json.dumps({"net_pnl_usd": net, "fees_usd": "0"}),
    )


def _record_funding_report(store: SqliteStore, *, settlement_ts: str, generated_at: str) -> None:
    store.save_scan(
        ScanRecord(
            scan_id="SCAN-DUE",
            symbol="BTCUSDT",
            period="4h",
            status="DONE",
            trigger_source="SCHEDULED",
            metrics_only=False,
            fixture_path="fixture",
            started_at="2026-10-08T00:00:00.000Z",
            finished_at=generated_at,
            window_start="2026-10-08T00:00:00.000Z",
            window_end="2026-10-08T04:00:00.000Z",
            trade_count=0,
            buy_volume="0",
            sell_volume="0",
            delta="0",
            cvd="0",
            data_quality="COMPLETE",
            manifest_path="manifest",
            parquet_rows=0,
            created_at=generated_at,
        )
    )
    store.save_report(
        ReportRecord(
            report_id="RPT-DUE",
            scan_id="SCAN-DUE",
            symbol="BTCUSDT",
            timeframe="4h",
            generated_at=generated_at,
            metrics_manifest_json="[]",
            llm_decision_id=None,
            narrative_ref=None,
            data_quality="COMPLETE",
            snapshot_json=json.dumps(
                {
                    "orderflow": {
                        "funding": {
                            "interval_hours": 8,
                            "history": [{"ts": settlement_ts, "rate": "0.0001"}],
                        }
                    }
                }
            ),
            metrics_json="{}",
        )
    )


def test_funding_settlement_in_window_reads_only_recorded_points(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    settlement = "2026-10-08T02:00:00.000Z"
    _record_funding_report(store, settlement_ts=settlement, generated_at="2026-10-08T02:30:00.000Z")
    service = _service(settings, store)

    assert (
        service._positions.funding_settlement_in_window(POSITION_ID, WINDOW_START, WINDOW_END)
        == settlement
    )
    # A different window has no settlement; a missing position and an invalid
    # window yield None without raising.
    assert (
        service._positions.funding_settlement_in_window(
            POSITION_ID, "2026-10-08T04:00:00.000Z", "2026-10-08T08:00:00.000Z"
        )
        is None
    )
    assert (
        service._positions.funding_settlement_in_window("POS-MISSING", WINDOW_START, WINDOW_END)
        is None
    )
    assert (
        service._positions.funding_settlement_in_window(POSITION_ID, WINDOW_END, WINDOW_START)
        is None
    )
    # The real due logic reports the condition from the recorded settlement.
    assert FUNDING_SETTLEMENT_CONDITION in _due(service).conditions


def test_funding_settlement_in_window_ignores_a_non_open_position(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload(state="CLOSED", closed_at=NOW_TEXT))
    service = _service(settings, store)

    assert (
        service._positions.funding_settlement_in_window(POSITION_ID, WINDOW_START, WINDOW_END)
        is None
    )


def test_the_risk_breach_uses_the_persisted_caps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a persisted tighter daily-loss cap changes the due trigger."""
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    # Equity is 10,700; a 150 loss is between the persisted 1% (~107) and the
    # configured 3% (~321) caps.
    _seed_day_start(store, NOW.date(), "10850")
    store.set_system_state("risk_caps", json.dumps({"daily_loss_pct": 1.0}), updated_at=NOW_TEXT)
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.daily_loss == Decimal("150")
    assert RISK_BREACH_CONDITION in _due(service).conditions


def test_corrupt_persisted_caps_logs_and_reports_no_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _seed_day_start(store, NOW.date(), "12000")
    store.set_system_state("risk_caps", "{not-json", updated_at=NOW_TEXT)
    service = _service(settings, store)
    recorder = _LogRecorder()
    monkeypatch.setattr(management_service_module, "_LOGGER", recorder)

    due = _due(service)

    assert due.due is True
    assert RISK_BREACH_CONDITION not in due.conditions
    assert any(event == "management_risk_caps_unavailable" for event, _ in recorder.events)


def test_a_new_utc_day_reseeds_the_snapshot_without_inherited_drawdown(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    _seed_day_start(store, date(2026, 10, 7), "99999")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is True
    assert context.daily_loss == Decimal("0")
    snapshot = json.loads(store.get_system_state(DAILY_EQUITY_STATE_KEY) or "{}")
    assert snapshot["day"] == "2026-10-08"
    assert Decimal(snapshot["equity"]) == context.equity


def test_a_corrupt_day_start_snapshot_is_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    _insert_position(settings, _payload())
    store.set_system_state(DAILY_EQUITY_STATE_KEY, "not-json", updated_at=NOW_TEXT)
    service = _service(settings, store)
    recorder = _LogRecorder()
    monkeypatch.setattr(management_service_module, "_LOGGER", recorder)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is False
    assert context.daily_loss is None
    assert context.degraded_reason == "the UTC day-start equity snapshot is unavailable"
    assert any(event == "management_day_start_equity_corrupt" for event, _ in recorder.events)
