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

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.kill_switch import KillSwitch
from oft.application.management_service import (
    CADENCE_CONDITION,
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
        "mark_as_of": NOW_TEXT,
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
    _insert_position(settings, _payload(mark_as_of=stale_at))
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
    _insert_position(settings, _payload(mark_price="69999.00", mark_as_of=NOW_TEXT))
    _insert_trade(settings, "TRADE-LOSS", "POS-CLOSED-1", NOW_TEXT, "-400.00")
    service = _service(settings, store)

    context = service.risk_context(now=NOW_TEXT)

    assert context.measured is True
    # A tiny unrealized loss on top of the realized loss still breaches.
    assert context.daily_loss == Decimal("400.01000")
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
    # Each position loses 200 (0.01 * (50,000 - 70,000)), below the ~315 cap
    # measured with one position; together they lose 400, above the ~330 cap.
    _insert_position(
        settings,
        _payload(
            position_id="POS-A",
            mark_price="50000.00",
            mark_as_of=NOW_TEXT,
        ),
    )
    service = _service(settings, store)
    assert RISK_BREACH_CONDITION not in _due(service, "POS-A").conditions

    _insert_position(
        settings,
        _payload(
            position_id="POS-B",
            mark_price="50000.00",
            mark_as_of=NOW_TEXT,
        ),
    )
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


def test_daily_loss_breach_is_strictly_greater_than_the_configured_pct() -> None:
    assert daily_loss_breach(equity=Decimal("10000"), loss=Decimal("301"), risk_pct=3.0) is True
    assert daily_loss_breach(equity=Decimal("10000"), loss=Decimal("300"), risk_pct=3.0) is False
    assert daily_loss_breach(equity=Decimal("0"), loss=Decimal("1"), risk_pct=3.0) is False


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
