"""Integration: partial close and add update one OPEN position; full close totals it."""

from __future__ import annotations

import json
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
from oft.application.paper_broker import BrokerError, OrderIntent, PaperBroker
from oft.application.position_service import PositionService
from oft.application.scan_service import ScanRequest, ScanService
from oft.application.trigger_service import TriggerService
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.oms import OrderSide, OrderType, PositionSide
from oft.domain.pnl import quantize_money, return_percent
from oft.domain.positions import TradeLogContext
from oft.domain.resampling import Bar
from oft.domain.time import utc_timestamp
from oft.ports.store import DecisionRecord, RiskVerdictRecord
from position_support import set_position_mark

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "tests" / "fixtures" / "archive"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _build_loop(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        archive_fixture_dir=ARCHIVE,
        llm_provider="stub",
        llm_fixtures_dir=FIXTURES,
    )
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
    accepted = entry.accept(run.trigger.trigger_id, idempotency_key="partial-entry")
    assert accepted.position is not None
    return {
        "container": container,
        "position": accepted.position,
        "order": accepted.order,
    }


def _broker(container: Any) -> PaperBroker:
    return PaperBroker(settings=container.settings, store=container.store, clock=container.clock)


def _intent(
    position: dict[str, Any],
    *,
    purpose: str,
    quantity: str,
    side: OrderSide | None = None,
    idempotency_key: str,
    risk_verdict_id: str = "RISK-MGMT",
    decision_id: str | None = None,
    trade_log: TradeLogContext | None = None,
) -> OrderIntent:
    position_side = PositionSide(str(position["side"]))
    if side is None:
        if purpose == "EXIT":
            side = OrderSide.SELL if position_side is PositionSide.LONG else OrderSide.BUY
        else:
            side = OrderSide.BUY if position_side is PositionSide.LONG else OrderSide.SELL
    return OrderIntent(
        symbol=position["symbol"],
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        price=None,
        stop_price=Decimal(str(position["stop_price"])),
        take_profits=(),
        leverage=float(position["leverage"]),
        decision_id=decision_id or f"DEC-{idempotency_key}",
        risk_verdict_id=risk_verdict_id,
        approval_id="APR-MGMT",
        idempotency_key=idempotency_key,
        position_side=position_side,
        risk_amount_usd=Decimal("0"),
        risk_pct=0.0,
        purpose=purpose,
        position_id=position["position_id"],
        trigger_id=position.get("source_trigger_id"),
        trade_log=trade_log,
    )


def _persist_verdict(
    container: Any,
    position: dict[str, Any],
    *,
    decision_id: str,
    risk_verdict_id: str,
    verdict: str = "ALLOW",
    allowed_quantity: str | None = None,
    allowed_leverage: float | None = None,
    expires_at: str | None = None,
) -> None:
    """Persist a verdict bound to the position's trigger and add decision.

    The default is a usable ``ALLOW`` that covers the original position size
    and leverage; the negative tests override one dimension at a time.
    """
    container.store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role="POSITION_MANAGEMENT",
            decision_type="POSITION_MANAGEMENT",
            validation_status="VALID",
            raw_output_hash="0" * 64,
            persisted_at=position["updated_at"],
            payload_json="{}",
        )
    )
    allowed = None if verdict == "BLOCK" else (allowed_quantity or str(Decimal(position["size"])))
    leverage = float(position["leverage"]) if allowed_leverage is None else allowed_leverage
    expiry = expires_at or utc_timestamp(NOW + timedelta(hours=8))
    payload = {
        "verdict": verdict,
        "allowed": {
            "quantity": allowed,
            "notional_usd": None,
            "leverage": leverage,
            "risk_amount_usd": "0",
        },
        "expires_at": expiry,
    }
    container.store.save_risk_verdict(
        RiskVerdictRecord(
            risk_verdict_id=risk_verdict_id,
            trigger_id=position["source_trigger_id"],
            decision_id=decision_id,
            verdict=verdict,
            binding_cap="NONE",
            computed_at=position["updated_at"],
            expires_at=expiry,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )


def _seed_bar(
    container: Any,
    *,
    low: Decimal,
    high: Decimal,
    close: Decimal,
    bar_end: datetime,
    run_id: str,
) -> None:
    """Seed one published bar for the symbol's existing series."""
    period, metric_version = _series(container)
    bar = Bar(
        symbol="BTCUSDT",
        period=period,
        bar_start=bar_end - timedelta(hours=4),
        bar_end=bar_end,
        open=close,
        high=high,
        low=low,
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
        window_start=bar.bar_start,
        window_end=bar_end,
        metric_version=metric_version,
        input_manifest_hash="partial-bar-input",
        source_mode="BACKFILL",
        source_digest="partial-bar-digest",
        run_id=run_id,
        computed_at=utc_timestamp(bar_end),
        data_quality="COMPLETE",
        quality_reasons=(),
    )
    container.store.upsert_manifest_entry(entry)


def _series(container: Any) -> tuple[str, str]:
    for entry in container.store.list_manifest():
        if entry.data_type != "bars" or entry.symbol != "BTCUSDT" or not entry.metric_version:
            continue
        if "/period=" in entry.path:
            period = entry.path.split("/period=", 1)[1].split("/", 1)[0]
            return period, entry.metric_version
    raise AssertionError("the scan did not publish a bars series")


def _mark(position: dict[str, Any]) -> Decimal:
    return Decimal(str(position["mark_price"]))


def _cash(container: Any) -> Decimal:
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None
    return Decimal(account.balance)


def _payload(container: Any, position_id: str) -> dict[str, Any]:
    record = container.store.get_position(position_id)
    assert record is not None
    return json.loads(record.payload_json)


def test_a_half_partial_close_keeps_the_position_open_and_writes_no_trade_log(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    size = Decimal(position["size"])
    half = size / Decimal("2")
    cash_before = _cash(container)

    result = _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="partial-1",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )

    assert result.position.state == "OPEN"
    assert result.position.size == size - half
    payload = _payload(container, position_id)
    assert payload["state"] == "OPEN"
    assert payload["closed_at"] is None
    assert payload["exit_price"] is None
    assert Decimal(payload["realized_pnl_usd"]) != 0
    assert container.store.list_trade_logs(limit=None) == ()
    assert container.store.count_positions() == 1
    assert [event.action for event in container.store.list_position_events(position_id)] == [
        "PARTIAL_CLOSE"
    ]
    # A LONG partial close credits the sale proceeds minus the fee.
    fill = container.store.get_fill_by_order_id(result.order.order_id)
    assert fill is not None
    fill_payload = json.loads(fill.payload_json)
    assert _cash(container) - cash_before == (
        Decimal(fill_payload["quote_quantity"]) - Decimal(fill_payload["fee"])
    ).quantize(Decimal("0.00000001"))


def test_a_full_close_after_a_partial_close_creates_one_cumulative_trade_log(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    size = Decimal(position["size"])
    half = size / Decimal("2")
    entry_fee = Decimal(
        json.loads(container.store.get_fill_by_order_id(loop["order"]["order_id"]).payload_json)[
            "fee"
        ]
    )

    partial = _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="partial-2",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )
    partial_fill = container.store.get_fill_by_order_id(partial.order.order_id)
    assert partial_fill is not None
    partial_payload = json.loads(partial_fill.payload_json)

    set_position_mark(
        container,
        position_id,
        position["stop_price"],
        as_of=position["updated_at"],
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    closed = service.monitor(position_id)

    assert closed.status == "CLOSED"
    assert closed.trade_log is not None
    assert closed.order is not None
    trade = closed.trade_log
    exit_fill = container.store.get_fill_by_order_id(closed.order["order_id"])
    assert exit_fill is not None
    exit_payload = json.loads(exit_fill.payload_json)

    entry_avg = Decimal(position["avg_entry_price"])
    first_gross = (Decimal(partial_payload["price"]) - entry_avg) * half
    second_gross = (Decimal(exit_payload["price"]) - entry_avg) * (size - half)
    expected_gross = (first_gross + second_gross).quantize(Decimal("0.00000001"))
    expected_fees = (
        entry_fee + Decimal(partial_payload["fee"]) + Decimal(exit_payload["fee"])
    ).quantize(Decimal("0.00000001"))
    assert Decimal(trade["gross_pnl_usd"]) == expected_gross
    assert Decimal(trade["fees_usd"]) == expected_fees
    assert Decimal(trade["net_pnl_usd"]) == (expected_gross - expected_fees).quantize(
        Decimal("0.00000001")
    )
    # The journal carries the total traded size and the PARTIAL outcome, not the
    # residual final-close size.
    assert Decimal(trade["size"]) == size
    assert "PARTIAL" in trade["outcome_tags"]
    entry_fill_payload = json.loads(
        container.store.get_fill_by_order_id(loop["order"]["order_id"]).payload_json
    )
    entry_notional = Decimal(entry_fill_payload["quote_quantity"])
    assert trade["return_pct"] == return_percent(Decimal(trade["net_pnl_usd"]), entry_notional)
    assert len(container.store.list_trade_logs(limit=None)) == 1
    assert [event.action for event in container.store.list_position_events(position_id)] == [
        "PARTIAL_CLOSE",
        "FULL_CLOSE",
    ]


def test_an_add_increases_size_weighted_avg_and_frozen_risk_without_a_trade_log(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    size = Decimal(position["size"])
    add_qty = size
    risk_before = Decimal(position["risk_amount_usd"])
    cash_before = _cash(container)
    _persist_verdict(
        container,
        position,
        decision_id="DEC-add-1",
        risk_verdict_id="RISK-add-1",
    )

    result = _broker(container).submit(
        _intent(
            position,
            purpose="ADD",
            quantity=str(add_qty),
            idempotency_key="add-1",
            risk_verdict_id="RISK-add-1",
        ),
        mark_price=_mark(position) + Decimal("500"),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )

    payload = _payload(container, position_id)
    assert result.position.state == "OPEN"
    assert Decimal(payload["size"]) == size + add_qty
    assert Decimal(payload["avg_entry_price"]) > Decimal(position["avg_entry_price"])
    add_order = result.order
    add_fill = container.store.get_fill_by_order_id(add_order.order_id)
    assert add_fill is not None
    add_payload = json.loads(add_fill.payload_json)
    expected_risk = (
        risk_before + abs(Decimal(add_payload["price"]) - Decimal(position["stop_price"])) * add_qty
    )
    assert Decimal(payload["risk_amount_usd"]) == expected_risk.quantize(Decimal("0.00000001"))
    assert container.store.list_trade_logs(limit=None) == ()
    assert container.store.count_positions() == 1
    assert [event.action for event in container.store.list_position_events(position_id)] == ["ADD"]
    # A LONG add debits the cost plus the fee.
    assert _cash(container) - cash_before == -(
        Decimal(add_payload["quote_quantity"]) + Decimal(add_payload["fee"])
    ).quantize(Decimal("0.00000001"))


def test_a_partial_close_larger_than_the_position_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    position = loop["position"]
    assert position is not None
    intent = _intent(
        position,
        purpose="EXIT",
        quantity=str(Decimal(position["size"]) + Decimal("1")),
        idempotency_key="too-big",
        risk_verdict_id=loop["order"]["risk_verdict_id"],
    )
    with pytest.raises(BrokerError):
        _broker(loop["container"]).submit(
            intent,
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )


def test_account_summary_uses_the_accumulated_realized_values(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    half = Decimal(position["size"]) / Decimal("2")
    _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="partial-3",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )

    summary = PaperAccountService(
        settings=container.settings, store=container.store, clock=container.clock
    ).summary()

    assert summary["open_positions"] == 1
    assert summary["closed_trades"] == 0


def test_partial_close_event_is_append_only(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    half = Decimal(position["size"]) / Decimal("2")
    _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="partial-4",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE position_events SET note = 'rewritten' WHERE position_id = ?",
            (position["position_id"],),
        )


def test_an_add_after_a_partial_close_keeps_the_cumulative_gross(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    size = Decimal(position["size"])
    half = size / Decimal("2")
    entry_avg = Decimal(position["avg_entry_price"])
    broker = _broker(container)

    partial = broker.submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="h1-partial",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )
    partial_fill = container.store.get_fill_by_order_id(partial.order.order_id)
    assert partial_fill is not None
    partial_payload = json.loads(partial_fill.payload_json)
    partial_gross = quantize_money((Decimal(partial_payload["price"]) - entry_avg) * half)
    after_partial = _payload(container, position_id)
    assert Decimal(after_partial["realized_pnl_usd"]) == partial_gross

    _persist_verdict(container, position, decision_id="DEC-h1-add", risk_verdict_id="RISK-h1-add")
    add = broker.submit(
        _intent(
            position,
            purpose="ADD",
            quantity=str(size),
            idempotency_key="h1-add",
            risk_verdict_id="RISK-h1-add",
            decision_id="DEC-h1-add",
        ),
        mark_price=_mark(position) + Decimal("500"),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )
    after_add = _payload(container, position_id)
    assert Decimal(after_add["realized_pnl_usd"]) == partial_gross

    add_fill = container.store.get_fill_by_order_id(add.order.order_id)
    assert add_fill is not None
    add_payload = json.loads(add_fill.payload_json)
    current_size = size - half
    weighted = quantize_money(
        (entry_avg * current_size + Decimal(add_payload["price"]) * size) / (current_size + size)
    )
    set_position_mark(
        container,
        position_id,
        position["stop_price"],
        as_of=utc_timestamp(NOW),
    )
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )
    closed = service.monitor(position_id)

    assert closed.status == "CLOSED" and closed.trade_log is not None
    exit_order = closed.order
    assert exit_order is not None
    exit_fill = container.store.get_fill_by_order_id(exit_order["order_id"])
    assert exit_fill is not None
    exit_payload = json.loads(exit_fill.payload_json)
    remaining = size + size - half
    expected = quantize_money(
        partial_gross + (Decimal(exit_payload["price"]) - weighted) * remaining
    )
    assert Decimal(closed.trade_log["gross_pnl_usd"]) == expected


def test_a_partial_close_that_is_not_a_step_multiple_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    size = Decimal(position["size"])
    quantity = size - Decimal("0.0005")
    assert quantity > 0 and quantity < size

    with pytest.raises(BrokerError) as exc:
        _broker(container).submit(
            _intent(
                position,
                purpose="EXIT",
                quantity=str(quantity),
                idempotency_key="bad-step",
                risk_verdict_id=loop["order"]["risk_verdict_id"],
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert exc.value.code == "ORDER_REJECTED"


def test_a_replayed_add_returns_the_committed_result(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    size = Decimal(position["size"])
    _persist_verdict(
        container, position, decision_id="DEC-add-replay", risk_verdict_id="RISK-add-replay"
    )
    intent = _intent(
        position,
        purpose="ADD",
        quantity=str(size),
        idempotency_key="add-replay",
        risk_verdict_id="RISK-add-replay",
        decision_id="DEC-add-replay",
    )
    broker = _broker(container)
    first = broker.submit(
        intent,
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )
    cash_after_first = _cash(container)
    fills_after_first = container.store.count_fills()

    second = broker.submit(
        intent,
        mark_price=_mark(position),
        mark_as_of=position["updated_at"],
        now=position["updated_at"],
    )

    assert second.order.order_id == first.order.order_id
    assert second.fill.fill_id == first.fill.fill_id
    assert _cash(container) == cash_after_first
    assert container.store.count_fills() == fills_after_first


def test_an_add_without_a_bound_verdict_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    with pytest.raises(BrokerError) as exc:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(position["size"]),
                idempotency_key="add-no-verdict",
                risk_verdict_id="RISK-missing",
                decision_id="DEC-add-no-verdict",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert exc.value.code == "RISK_VERDICT_MISSING"

    # A verdict bound to a different decision is not a valid add gate either.
    _persist_verdict(container, position, decision_id="DEC-other", risk_verdict_id="RISK-other")
    with pytest.raises(BrokerError) as mismatch:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(position["size"]),
                idempotency_key="add-wrong-verdict",
                risk_verdict_id="RISK-other",
                decision_id="DEC-add-wrong-verdict",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert mismatch.value.code == "RISK_VERDICT_INVALID"


def test_a_blocked_or_expired_add_verdict_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    size = Decimal(position["size"])

    _persist_verdict(
        container,
        position,
        decision_id="DEC-add-block",
        risk_verdict_id="RISK-add-block",
        verdict="BLOCK",
    )
    with pytest.raises(BrokerError) as blocked:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(size),
                idempotency_key="add-blocked",
                risk_verdict_id="RISK-add-block",
                decision_id="DEC-add-block",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert blocked.value.code == "RISK_BLOCKED"

    _persist_verdict(
        container,
        position,
        decision_id="DEC-add-expired",
        risk_verdict_id="RISK-add-expired",
        expires_at=utc_timestamp(NOW - timedelta(seconds=1)),
    )
    with pytest.raises(BrokerError) as expired:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(size),
                idempotency_key="add-expired",
                risk_verdict_id="RISK-add-expired",
                decision_id="DEC-add-expired",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert expired.value.code == "RISK_BLOCKED"


def test_an_add_above_the_verdict_caps_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    size = Decimal(position["size"])

    _persist_verdict(
        container,
        position,
        decision_id="DEC-add-cap",
        risk_verdict_id="RISK-add-cap",
        allowed_quantity=str(size / Decimal("2")),
    )
    with pytest.raises(BrokerError) as over_quantity:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(size),
                idempotency_key="add-over-cap",
                risk_verdict_id="RISK-add-cap",
                decision_id="DEC-add-cap",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert over_quantity.value.code == "RISK_VERDICT_INVALID"

    _persist_verdict(
        container,
        position,
        decision_id="DEC-add-lev",
        risk_verdict_id="RISK-add-lev",
        allowed_leverage=float(position["leverage"]) / 2,
    )
    with pytest.raises(BrokerError) as over_leverage:
        _broker(container).submit(
            _intent(
                position,
                purpose="ADD",
                quantity=str(size),
                idempotency_key="add-over-leverage",
                risk_verdict_id="RISK-add-lev",
                decision_id="DEC-add-lev",
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert over_leverage.value.code == "RISK_VERDICT_INVALID"


def test_an_implausible_recorded_bar_close_never_crashes_the_monitor(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    period, version = _series(container)
    # A corrupt-but-finite bar close must be ignored, not turned into a mark
    # that overflows the money math during the exit verdict.
    observation = PositionService._latest_valid_bar_row(
        "BTCUSDT",
        ({"bar_end": utc_timestamp(NOW), "close": "1e100"},),
        NOW,
    )
    assert observation is None
    _seed_bar(
        container,
        low=Decimal("1"),
        high=Decimal("1e100"),
        close=Decimal("1e100"),
        bar_end=NOW + timedelta(hours=1),
        run_id="corrupt-close-bar",
    )
    set_position_mark(
        container,
        position_id,
        position["mark_price"],
        as_of=utc_timestamp(NOW + timedelta(hours=2)),
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    result = service.monitor(position_id)

    assert result.status == "NO_ACTION"
    assert container.store.list_trade_logs(limit=None) == ()


def test_a_trade_log_context_that_contradicts_the_position_is_rejected(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    context = TradeLogContext(
        signal={},
        provenance={},
        decision_quality={},
        mae_usd=Decimal("-1"),
        mfe_usd=Decimal("1"),
        risk_amount_usd=Decimal("1"),
        entry_notional_usd=Decimal(str(position["notional_usd"])),
        data_quality_status="COMPLETE",
        data_quality_reasons=(),
        stopped=True,
    )
    with pytest.raises(BrokerError) as exc:
        _broker(container).submit(
            _intent(
                position,
                purpose="EXIT",
                quantity=str(position["size"]),
                idempotency_key="bad-journal",
                risk_verdict_id=loop["order"]["risk_verdict_id"],
                trade_log=context,
            ),
            mark_price=_mark(position),
            mark_as_of=position["updated_at"],
            now=position["updated_at"],
        )
    assert exc.value.code == "TRADE_LOG_INVALID"
    assert container.store.list_trade_logs(limit=None) == ()


def test_mae_uses_the_size_in_force_before_a_partial_close(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    size = Decimal(position["size"])
    half = size / Decimal("2")
    entry = Decimal(position["avg_entry_price"])
    stop = Decimal(position["stop_price"])
    partial_at = utc_timestamp(NOW + timedelta(hours=1))

    _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="m6-partial",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=entry,
        mark_as_of=position["updated_at"],
        now=partial_at,
    )
    _seed_bar(
        container,
        low=entry - Decimal("5000"),
        high=entry + Decimal("100"),
        close=entry,
        bar_end=NOW,
        run_id="m6-before",
    )
    _seed_bar(
        container,
        low=entry - Decimal("10"),
        high=entry + Decimal("10"),
        close=entry,
        bar_end=NOW + timedelta(hours=1),
        run_id="m6-after",
    )
    _seed_bar(
        container,
        low=stop,
        high=entry,
        close=stop,
        bar_end=NOW + timedelta(hours=2),
        run_id="m6-close",
    )
    set_position_mark(container, position_id, position["stop_price"], as_of=utc_timestamp(NOW))
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    closed = service.monitor(position_id)

    assert closed.status == "CLOSED" and closed.trade_log is not None
    # The excursion before the partial close is measured on the full size.
    assert Decimal(closed.trade_log["mae_usd"]) == quantize_money(
        (entry - Decimal("5000") - entry) * size
    )
