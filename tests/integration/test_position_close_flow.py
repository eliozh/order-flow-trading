"""Integration: one stop touch closes the position and writes exactly one trade log."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.parquet_store import ParquetStore
from oft.application.entry_service import EntryService
from oft.application.kill_switch import KillSwitch
from oft.application.paper_account import PaperAccountService
from oft.application.paper_broker import OrderIntent, PaperBroker
from oft.application.position_service import PositionMonitorResult, PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.resampling import Bar
from oft.domain.time import utc_timestamp
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )


def _bars_series(container: Any) -> tuple[str, str]:
    """The period and metric version of the series the scan published."""
    for entry in container.store.list_manifest():
        if entry.data_type != "bars" or entry.symbol != "BTCUSDT" or not entry.metric_version:
            continue
        marker = "/period="
        if marker in entry.path:
            return entry.path.split(marker, 1)[1].split("/", 1)[0], entry.metric_version
    raise AssertionError("the scan did not publish a bars series")


def _seed_bar(
    container: Any,
    *,
    close: Decimal,
    bar_end: datetime,
    period: str,
    metric_version: str,
    computed_at: str | None = None,
    run_id: str = "test-fresh-bar",
) -> None:
    """Publish one recent recorded bar so the mark resolver can use it.

    ``computed_at`` is the manifest ``created_at`` (it defaults to the bar's
    end), so a test can make a series look newer in the manifest while its
    newest usable bar is older, and vice versa.
    """
    bar_start = bar_end - timedelta(hours=4)
    bar = Bar(
        symbol="BTCUSDT",
        period=period,
        bar_start=bar_start,
        bar_end=bar_end,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
        quote_volume=close,
        trade_count=1,
        taker_buy_volume=Decimal("0"),
        source_mode="BACKFILL",
    )
    entry = ParquetStore(container.settings).write_bar_part(
        (bar,),
        symbol="BTCUSDT",
        period=period,
        window_start=bar_start,
        window_end=bar_end,
        metric_version=metric_version,
        input_manifest_hash="test-input",
        source_mode="BACKFILL",
        source_digest="test-digest",
        run_id=run_id,
        computed_at=computed_at if computed_at is not None else utc_timestamp(bar_end),
        data_quality="COMPLETE",
        quality_reasons=(),
    )
    container.store.upsert_manifest_entry(entry)


def _setup(tmp_path: Path) -> dict[str, Any]:
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
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
    run = TriggerService(store=container.store, clock=container.clock).evaluate(
        opportunity.opportunity_id
    )
    assert run.trigger is not None
    kill = KillSwitch(settings=container.settings, store=container.store, clock=container.clock)
    entry = EntryService(
        store=container.store,
        clock=container.clock,
        provider=container.llm_provider,
        settings=container.settings,
        kill_switch=kill,
    )
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="flow-entry")
    assert accepted.position is not None
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    return {
        "container": container,
        "service": service,
        "position": accepted.position,
        "entry_order": accepted.order,
        "entry_fill": accepted.fill,
        "trigger_id": run.trigger.trigger_id,
    }


def _seed_short_entry_parents(container: Any) -> None:
    """Seed the FK parents a directly-submitted SHORT entry needs."""
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO opportunities "
            "(opportunity_id, symbol, state, created_at, updated_at, payload_json, expires_at) "
            "VALUES ('OPP-SHORT', 'BTCUSDT', 'TRIGGERED', 't', 't', '{}', NULL)"
        )
        connection.execute(
            "INSERT INTO trigger_events "
            "(trigger_id, opportunity_id, symbol, status, idempotency_key, triggered_at, "
            "payload_json) VALUES ('TRG-SHORT', 'OPP-SHORT', 'BTCUSDT', 'PENDING_RECHECK', "
            "'TRG-SHORT-KEY', 't', '{}')"
        )
        connection.execute(
            "INSERT INTO decisions "
            "(decision_id, role, decision_type, validation_status, raw_output_hash, "
            "persisted_at, payload_json) VALUES "
            "('DEC-SHORT', 'ENTRY_RECHECK', 'ENTRY_RECHECK', 'VALID', 'H', 't', '{}')"
        )
        connection.execute(
            "INSERT INTO risk_verdicts "
            "(risk_verdict_id, trigger_id, decision_id, verdict, binding_cap, computed_at, "
            "expires_at, payload_json) VALUES "
            "('RISK-SHORT', 'TRG-SHORT', 'DEC-SHORT', 'ALLOW', 'NONE', 't', 't', '{}')"
        )
        connection.commit()


def _short_setup(tmp_path: Path) -> dict[str, Any]:
    """Build a real OPEN SHORT position through the paper broker, no LLM."""
    settings = _settings(tmp_path)
    container = build_container(settings, initialize=True, clock=FixedClock(NOW))
    _seed_short_entry_parents(container)
    broker = PaperBroker(settings=settings, store=container.store, clock=container.clock)
    intent = OrderIntent(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        price=None,
        stop_price=Decimal("71000"),
        take_profits=(),
        leverage=2.0,
        decision_id="DEC-SHORT",
        risk_verdict_id="RISK-SHORT",
        approval_id="APR-SHORT",
        idempotency_key="short-entry",
        position_side=PositionSide.SHORT,
        risk_amount_usd=Decimal("10"),
        risk_pct=0.1,
        trigger_id="TRG-SHORT",
    )
    entered = broker.submit(
        intent,
        mark_price=Decimal("70000"),
        mark_as_of=utc_timestamp(NOW),
        now=utc_timestamp(NOW),
    )
    assert entered.position.side is PositionSide.SHORT
    service = PositionService(store=container.store, clock=container.clock, settings=settings)
    return {"container": container, "service": service, "position": entered.position.to_contract()}


def _cash(container: Any) -> Decimal:
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None
    return Decimal(account.balance)


def _starting_cash(container: Any) -> Decimal:
    """Cash before the entry leg, so the whole round trip is measured."""
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None
    return Decimal(account.initial_balance)


def test_stop_touch_creates_one_order_fill_closed_row_and_trade_log(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    stop = position["stop_price"]
    set_position_mark(container, position_id, stop, as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.status == "CLOSED"
    assert result.reason_code == "STOP_TOUCH"
    assert result.action == "FULL_CLOSE"
    assert result.trade_log is not None
    assert result.position is not None
    assert result.position["state"] == "CLOSED"
    assert result.position["closed_at"] is not None
    assert result.position["exit_price"] is not None
    assert result.position["realized_pnl_usd"] == result.trade_log["net_pnl_usd"]

    orders = container.store.list_orders()
    fills = container.store.list_fills()
    assert len(orders) == 2  # entry + exit
    assert len(fills) == 2
    exit_order = next(order for order in orders if order.purpose == "EXIT")
    exit_payload = json.loads(exit_order.payload_json)
    assert exit_payload["reduce_only"] is True
    assert exit_payload["position_id"] == position_id
    assert exit_payload["status"] == "FILLED"
    assert exit_order.trigger_id == setup["trigger_id"]

    assert container.store.count_positions() == 1
    assert container.store.list_positions(state="OPEN") == ()
    closed = container.store.list_positions(state="CLOSED")
    assert len(closed) == 1
    assert json.loads(closed[0].payload_json)["position_id"] == position_id

    trades = container.store.list_trade_logs(limit=None)
    assert len(trades) == 1
    assert trades[0].position_id == position_id
    assert container.store.get_trade_log_for_position(position_id) is not None

    events = container.store.list_position_events(position_id)
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].action == "FULL_CLOSE"
    assert events[0].order_id == exit_payload["order_id"]

    audits = [event.event_type for event in container.store.list_audit_events(limit=100)]
    assert audits.count("POSITION_CLOSED") == 1
    assert audits.count("TRADE_LOGGED") == 1
    assert audits.count("INTENT_CLASSIFIED") >= 2  # the entry and the exit

    verdict = container.store.get_risk_verdict(exit_order.risk_verdict_id)
    assert verdict is not None
    assert json.loads(verdict.payload_json)["verdict"] in {"ALLOW", "REDUCE"}


def test_trade_log_accumulates_entry_and_exit_fees(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)
    assert result.trade_log is not None

    entry_fill = container.store.get_fill_by_order_id(setup["entry_order"]["order_id"])
    exit_order = next(order for order in container.store.list_orders() if order.purpose == "EXIT")
    exit_fill = container.store.get_fill_by_order_id(exit_order.order_id)
    assert entry_fill is not None and exit_fill is not None
    expected_fees = Decimal(json.loads(entry_fill.payload_json)["fee"]) + Decimal(
        json.loads(exit_fill.payload_json)["fee"]
    )
    # Both terms are quantized to 8 dp; the sum is exact at that precision.
    assert Decimal(result.trade_log["fees_usd"]) == expected_fees
    assert result.trade_log["funding_usd"] == "0"
    # No recorded funding settlement falls inside this position's holding
    # window, so funding is a measured zero and is not a quality gap. The
    # journal is PARTIAL only because no recorded bars cover the window.
    assert result.trade_log["data_quality"]["status"] == "PARTIAL"
    assert any("MAE/MFE" in reason for reason in result.trade_log["data_quality"]["reasons"])
    assert not any("funding" in reason for reason in result.trade_log["data_quality"]["reasons"])


def test_a_mark_above_the_stop_creates_no_order(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    mark = str(float(position["stop_price"]) + 1)
    set_position_mark(container, position_id, mark, as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.status == "NO_ACTION"
    assert result.reason_code == "STOP_NOT_TOUCHED"
    assert result.order is None and result.trade_log is None
    assert len(container.store.list_orders()) == 1
    assert container.store.list_trade_logs(limit=None) == ()
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_a_stale_mark_creates_no_order_and_reports_data_stale(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    stale_at = utc_timestamp(NOW - timedelta(seconds=120))
    set_position_mark(container, position_id, position["stop_price"], as_of=stale_at)

    result = setup["service"].monitor(position_id)

    assert result.status == "NO_ACTION"
    assert result.reason_code == "DATA_STALE"
    assert result.reason is not None and "older than" in result.reason
    assert result.order is None and result.fill is None
    assert len(container.store.list_orders()) == 1
    assert container.store.list_trade_logs(limit=None) == ()
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_a_missing_mark_is_reported_as_data_stale(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, None, as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.status == "NO_ACTION"
    assert result.reason_code == "DATA_STALE"
    assert len(container.store.list_orders()) == 1


def test_the_close_goes_through_the_execution_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))
    service = setup["service"]
    calls: list[dict[str, Any]] = []
    original = service._gateway.submit

    def _submit(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._gateway, "submit", _submit)

    result = service.monitor(position_id)

    assert len(calls) == 1
    assert result.order is not None
    exit_order = container.store.get_order(result.order["order_id"])
    assert exit_order is not None
    assert container.store.get_risk_verdict(exit_order.risk_verdict_id) is not None


def test_paper_equity_reports_realized_pnl_and_closed_fees(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)
    assert result.trade_log is not None
    summary = PaperAccountService(
        settings=container.settings, store=container.store, clock=container.clock
    ).summary()

    assert summary["realized_pnl_usd"] == result.trade_log["net_pnl_usd"]
    assert summary["fees_usd"] == result.trade_log["fees_usd"]
    assert summary["closed_trades"] == 1
    assert summary["open_positions"] == 0
    # Cash already includes the realized close; equity equals cash with no marks.
    assert summary["equity"] == summary["cash_balance"]


def test_a_long_full_close_changes_cash_by_exactly_its_net_pnl(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    cash_before_entry = _starting_cash(container)
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.trade_log is not None
    net = Decimal(result.trade_log["net_pnl_usd"])
    assert _cash(container) - cash_before_entry == net


def test_a_short_full_close_changes_cash_by_exactly_its_net_pnl(tmp_path: Path) -> None:
    setup = _short_setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    cash_before_entry = _starting_cash(container)
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.status == "CLOSED"
    assert result.trade_log is not None
    net = Decimal(result.trade_log["net_pnl_usd"])
    # A full SHORT round trip credits the sale proceeds and debits the
    # buy-back plus fee, so cash moves by exactly gross - fees.
    assert _cash(container) - cash_before_entry == net
    assert Decimal(result.position["realized_pnl_usd"]) == net


def test_an_open_short_entry_does_not_overstate_equity(tmp_path: Path) -> None:
    setup = _short_setup(tmp_path)
    container = setup["container"]
    position = setup["position"]

    summary = PaperAccountService(
        settings=container.settings, store=container.store, clock=container.clock
    ).summary()

    cash = Decimal(summary["cash_balance"])
    liability = Decimal(position["size"]) * Decimal(position["mark_price"])
    marked = Decimal(summary["unrealized_at_last_mark"])
    # An open SHORT is a liability, not an asset: equity is cash minus the
    # marked buy-back cost, never cash plus the entry notional.
    assert summary["open_positions"] == 1
    assert marked == -liability
    assert Decimal(summary["equity"]) == cash + marked
    assert Decimal(summary["equity"]) == (cash - liability).quantize(Decimal("0.00000001"))
    assert Decimal(summary["equity"]) < cash


def test_a_fresh_account_reports_zero_totals_as_plain_decimals(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]

    summary = PaperAccountService(
        settings=container.settings, store=container.store, clock=container.clock
    ).summary()

    assert summary["closed_trades"] == 0
    assert summary["realized_pnl_usd"] == "0"
    assert summary["fees_usd"] == "0"
    assert "E" not in summary["realized_pnl_usd"]
    assert "E" not in summary["fees_usd"]


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        "this is not json",
        '{"net_pnl_usd": "not-a-number", "fees_usd": "0"}',
        "[]",
        "null",
        '"x"',
        "{}",
        '{"net_pnl_usd": "1.00"}',
    ],
)
def test_corrupt_trade_log_payloads_are_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, corrupt_payload: str
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        as_of=utc_timestamp(NOW),
    )
    result = setup["service"].monitor(position["position_id"])
    assert result.trade_log is not None
    trade_id = result.trade_log["trade_id"]
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trade_logs SET payload_json = ? WHERE trade_id = ?",
            (corrupt_payload, trade_id),
        )
        connection.commit()

    with caplog.at_level(logging.WARNING):
        summary = PaperAccountService(
            settings=container.settings, store=container.store, clock=container.clock
        ).summary()

    # One deterministic corruption policy: skip the row, keep the totals
    # usable, and name the trade id in a warning instead of swallowing it.
    assert summary["realized_pnl_usd"] == "0"
    assert summary["fees_usd"] == "0"
    assert summary["closed_trades"] == 0
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(trade_id in record.getMessage() for record in warnings)


# -- recorded-bar mark lookup (public surfaces only) -------------------------


def _advance(setup: dict[str, Any], seconds: int) -> PositionService:
    return PositionService(
        store=setup["container"].store,
        clock=FixedClock(NOW + timedelta(seconds=seconds)),
        settings=setup["container"].settings,
    )


def test_a_fresh_recorded_bar_at_the_stop_closes_through_public_surfaces(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]),
        bar_end=NOW,
        period=period,
        metric_version=version,
    )

    result = setup["service"].monitor(position_id)

    assert result.status == "CLOSED"
    assert result.reason_code == "STOP_TOUCH"
    assert result.trade_log is not None
    assert len(container.store.list_orders()) == 2  # entry + exactly one exit
    assert len(container.store.list_fills()) == 2
    assert len(container.store.list_trade_logs(limit=None)) == 1
    assert container.store.list_positions(state="OPEN") == ()
    assert len(container.store.list_positions(state="CLOSED")) == 1
    assert result.position["state"] == "CLOSED"


def test_a_fresh_recorded_bar_above_the_stop_creates_no_order(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]) + Decimal("1000"),
        bar_end=NOW,
        period=period,
        metric_version=version,
    )

    result = setup["service"].monitor(position["position_id"])

    assert result.status == "NO_ACTION"
    assert result.reason_code == "STOP_NOT_TOUCHED"
    assert len(container.store.list_orders()) == 1
    assert container.store.list_trade_logs(limit=None) == ()


def test_a_stale_recorded_bar_is_data_stale(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    period, version = _bars_series(container)
    set_position_mark(
        container,
        position["position_id"],
        str(Decimal(position["stop_price"]) + Decimal("1000")),
        as_of=utc_timestamp(NOW - timedelta(days=1)),
    )
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]),
        bar_end=NOW,
        period=period,
        metric_version=version,
    )

    service = _advance(setup, int(container.settings.risk_stale_mark_seconds) + 1)
    result = service.monitor(position["position_id"])

    assert result.status == "NO_ACTION"
    assert result.reason_code == "DATA_STALE"
    assert len(container.store.list_orders()) == 1


def test_the_freshest_series_wins_even_when_another_series_is_newer_in_the_manifest(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    period, version = _bars_series(container)
    # The stored fallback is stale and away from the stop, so it can never mask
    # a bar-derived observation.
    set_position_mark(
        container,
        position_id,
        str(Decimal(position["stop_price"]) + Decimal("1000")),
        as_of=utc_timestamp(NOW - timedelta(days=1)),
    )
    # Manifest-newest series, but its latest usable bar is two hours old.
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]) + Decimal("1000"),
        bar_end=NOW - timedelta(hours=2),
        period=period,
        metric_version=version,
        computed_at=utc_timestamp(NOW),
        run_id="test-shadowing-series-bar",
    )
    # Fresher series: an older manifest row, but its bar close is at NOW and
    # sits exactly on the stop.
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]),
        bar_end=NOW,
        period=period,
        metric_version="ofi.v1.0123456789abcdef",
        computed_at=utc_timestamp(NOW - timedelta(days=2)),
        run_id="test-fresher-series-bar",
    )

    result = setup["service"].monitor(position_id)

    assert result.status == "CLOSED"
    assert result.reason_code == "STOP_TOUCH"
    assert result.trade_log is not None
    assert len(container.store.list_orders()) == 2  # entry + exactly one exit
    assert len(container.store.list_trade_logs(limit=None)) == 1


def test_a_bar_as_of_exactly_at_the_ttl_boundary_is_fresh(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    period, version = _bars_series(container)
    ttl = int(container.settings.risk_stale_mark_seconds)
    set_position_mark(
        container,
        position["position_id"],
        str(Decimal(position["stop_price"]) + Decimal("1000")),
        as_of=utc_timestamp(NOW - timedelta(days=1)),
    )
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]) + Decimal("1000"),
        bar_end=NOW,
        period=period,
        metric_version=version,
    )

    result = _advance(setup, ttl).monitor(position["position_id"])

    assert result.reason_code == "STOP_NOT_TOUCHED"


def test_a_bar_as_of_one_second_past_the_ttl_is_stale(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    period, version = _bars_series(container)
    ttl = int(container.settings.risk_stale_mark_seconds)
    set_position_mark(
        container,
        position["position_id"],
        str(Decimal(position["stop_price"]) + Decimal("1000")),
        as_of=utc_timestamp(NOW - timedelta(days=1)),
    )
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]),
        bar_end=NOW,
        period=period,
        metric_version=version,
    )

    result = _advance(setup, ttl + 1).monitor(position["position_id"])

    assert result.reason_code == "DATA_STALE"
    assert len(container.store.list_orders()) == 1


def test_a_stored_mark_exactly_at_the_ttl_boundary_is_fresh(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    ttl = int(container.settings.risk_stale_mark_seconds)
    set_position_mark(
        container,
        position["position_id"],
        str(Decimal(position["stop_price"]) + Decimal("1000")),
        as_of=utc_timestamp(NOW - timedelta(seconds=ttl)),
    )

    result = setup["service"].monitor(position["position_id"])

    assert result.reason_code == "STOP_NOT_TOUCHED"


def test_a_stored_mark_one_second_past_the_ttl_is_stale(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    ttl = int(container.settings.risk_stale_mark_seconds)
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        as_of=utc_timestamp(NOW - timedelta(seconds=ttl + 1)),
    )

    result = setup["service"].monitor(position["position_id"])

    assert result.reason_code == "DATA_STALE"
    assert len(container.store.list_orders()) == 1


def test_an_unparseable_stored_mark_timestamp_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    # No usable bar may win the observation ranking, so the unparseable stored
    # as-of is the chosen observation and ``_is_stale``'s parse guard decides.
    monkeypatch.setattr(PositionService, "_latest_bar_mark", lambda self, position, now_dt: None)
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        as_of="not-a-timestamp",
    )

    result = setup["service"].monitor(position["position_id"])

    assert result.status == "NO_ACTION"
    assert result.reason_code == "DATA_STALE"
    assert result.reason is not None and "older than" in result.reason
    assert result.order is None and result.fill is None
    assert len(container.store.list_orders()) == 1
    assert container.store.list_trade_logs(limit=None) == ()
    assert len(container.store.list_positions(state="OPEN")) == 1


def test_one_decision_id_spans_history_decision_and_trade_log(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.position is not None
    history_decision_id = result.position["management_history"][-1]["decision_id"]
    trade = container.store.get_trade_log_for_position(position_id)
    assert trade is not None
    assert trade.decision_id == history_decision_id
    decision = container.store.get_decision(history_decision_id)
    assert decision is not None
    assert decision.role == "POSITION_MANAGEMENT"


def test_trade_log_column_matches_the_payload_money_string(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    container = setup["container"]
    position = setup["position"]
    position_id = position["position_id"]
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))

    result = setup["service"].monitor(position_id)

    assert result.trade_log is not None
    trade = container.store.get_trade_log_for_position(position_id)
    assert trade is not None
    assert trade.net_pnl_usd == result.trade_log["net_pnl_usd"]


def test_monitor_result_rejects_inconsistent_status_shapes() -> None:
    with pytest.raises(ValueError):
        PositionMonitorResult(
            position_id="POS-1",
            status="CLOSED",
            reason_code="STOP_TOUCH",
            action="FULL_CLOSE",
        )
    with pytest.raises(ValueError):
        PositionMonitorResult(
            position_id="POS-1",
            status="NO_ACTION",
            reason_code="DATA_STALE",
            action="FULL_CLOSE",
        )
    with pytest.raises(ValueError):
        PositionMonitorResult(
            position_id="POS-1",
            status="UNKNOWN",
            reason_code="DATA_STALE",
        )
