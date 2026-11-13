"""Integration: the full-close journal is complete and commits as one unit."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_funding_idempotency import _bars_series, _seed_bar
from test_partial_close_flow import NOW, _build_loop

from oft.application.position_service import PositionService
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    ClosedPositionRecord,
    FillRecord,
    OmsConflictError,
    OrderRecord,
    PaperAccountRecord,
    PositionEventRecord,
    TradeLogRecord,
)
from position_support import set_position_mark


def _close_with_bars(container: Any, position: dict[str, Any], service: Any) -> Any:
    period, version = _bars_series(container)
    stop = Decimal(position["stop_price"])
    _seed_bar(
        container,
        close=stop,
        bar_end=NOW,
        period=period,
        metric_version=version,
        run_id="journal-bar",
    )
    set_position_mark(
        container,
        position["position_id"],
        position["stop_price"],
        as_of=utc_timestamp(NOW),
    )
    return service.monitor(position["position_id"])


def test_full_close_trade_log_carries_every_contract_field(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )

    result = _close_with_bars(container, position, service)

    assert result.status == "CLOSED"
    trade = result.trade_log
    assert trade is not None
    for key in (
        "gross_pnl_usd",
        "fees_usd",
        "funding_usd",
        "net_pnl_usd",
        "r_multiple",
        "mae_usd",
        "mfe_usd",
        "return_pct",
        "outcome_tags",
        "data_quality",
        "provenance",
    ):
        assert key in trade
    assert Decimal(trade["net_pnl_usd"]) == (
        Decimal(trade["gross_pnl_usd"]) - Decimal(trade["fees_usd"]) + Decimal(trade["funding_usd"])
    ).quantize(Decimal("0.00000001"))
    assert Decimal(trade["mae_usd"]) <= 0
    assert Decimal(trade["mfe_usd"]) >= 0
    assert isinstance(trade["r_multiple"], float)
    assert isinstance(trade["return_pct"], float)
    assert trade["provenance"]["llm_provider"] == "stub"
    assert trade["provenance"]["calc_metric_version"] != ""
    assert trade["signal"]["trigger_id"] == position["source_trigger_id"]
    assert trade["signal"]["opportunity_id"] is not None
    assert trade["signal"]["decision_id"] is not None

    trade_record = container.store.get_trade_log_for_position(position["position_id"])
    assert trade_record is not None
    assert trade_record.net_pnl_usd == trade["net_pnl_usd"]
    events = container.store.list_position_events(position["position_id"])
    assert [event.action for event in events] == ["FULL_CLOSE"]
    audits = [event.event_type for event in container.store.list_audit_events(limit=100)]
    assert audits.count("POSITION_CLOSED") == 1
    assert audits.count("TRADE_LOGGED") == 1


def test_a_failed_trade_log_insert_rolls_back_the_whole_close(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    account = container.store.get_paper_account("ACC-PAPER")
    entry_order = loop["order"]
    stored_entry = container.store.get_order(entry_order["order_id"])
    assert stored_entry is not None and stored_entry.decision_id is not None
    entry_fill = container.store.get_fill_by_order_id(entry_order["order_id"])
    assert account is not None and entry_fill is not None
    now = utc_timestamp(NOW)
    # Seed a conflicting trade log for the same position so the close's own
    # trade-log insert violates the unique constraint after the position flip.
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_logs (trade_id, position_id, account_id, environment, symbol, "
            "side, opened_at, closed_at, net_pnl_usd, idempotency_key, payload_json, order_id, "
            "fill_id, risk_verdict_id, decision_id) "
            "VALUES ('TRD-CONFLICT', ?, ?, 'PAPER', ?, ?, ?, ?, '0', 'idem-conflict', '{}', ?, "
            "?, ?, ?)",
            (
                position_id,
                account.account_id,
                position["symbol"],
                position["side"],
                position["opened_at"],
                now,
                entry_order["order_id"],
                entry_fill.fill_id,
                stored_entry.risk_verdict_id,
                stored_entry.decision_id,
            ),
        )
        connection.commit()
    open_record = container.store.get_position(position_id)
    assert open_record is not None and open_record.state == "OPEN"

    rollback = ClosedPositionRecord(
        order=OrderRecord(
            order_id="ORD-ROLLBACK",
            client_order_id="client-rollback",
            environment="PAPER",
            symbol=position["symbol"],
            status="FILLED",
            risk_verdict_id=entry_order["risk_verdict_id"],
            idempotency_key="idem-rollback",
            created_at=now,
            updated_at=now,
            payload_json="{}",
            trigger_id=position["source_trigger_id"],
            decision_id=stored_entry.decision_id,
            purpose="EXIT",
        ),
        fill=FillRecord(
            fill_id="FIL-ROLLBACK",
            order_id="ORD-ROLLBACK",
            environment="PAPER",
            symbol=position["symbol"],
            filled_at=now,
            payload_json="{}",
        ),
        position=open_record,
        account=PaperAccountRecord(
            account_id=account.account_id,
            environment=account.environment,
            currency=account.currency,
            initial_balance=account.initial_balance,
            balance=account.balance,
            updated_at=now,
        ),
        balance_delta="1.00",
        event=PositionEventRecord(
            position_id=position_id,
            seq=None,
            at=now,
            action="FULL_CLOSE",
            order_id="ORD-ROLLBACK",
            note="rollback attempt",
        ),
        trade_log=TradeLogRecord(
            trade_id="TRD-ROLLBACK",
            position_id=position_id,
            account_id=account.account_id,
            environment="PAPER",
            symbol=position["symbol"],
            side=position["side"],
            opened_at=position["opened_at"],
            closed_at=now,
            net_pnl_usd="0.00000000",
            idempotency_key="idem-rollback",
            payload_json="{}",
            order_id="ORD-ROLLBACK",
            fill_id="FIL-ROLLBACK",
            risk_verdict_id=stored_entry.risk_verdict_id,
            decision_id=stored_entry.decision_id,
        ),
    )

    with pytest.raises(OmsConflictError):
        container.store.save_close(rollback)

    after = container.store.get_position(position_id)
    assert after is not None and after.state == "OPEN"
    assert container.store.get_order("ORD-ROLLBACK") is None
    assert container.store.get_fill_by_order_id("ORD-ROLLBACK") is None
    assert container.store.get_trade_log("TRD-ROLLBACK") is None
    assert container.store.list_position_events(position_id) == ()
    after_account = container.store.get_paper_account("ACC-PAPER")
    assert after_account is not None and after_account.balance == account.balance


def _rewrite_payload(container: Any, position_id: str, **changes: Any) -> None:
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(changes)
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


def test_a_zero_frozen_risk_journals_partial_with_the_r_reason(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    _rewrite_payload(container, position_id, risk_amount_usd="0")
    service = PositionService(
        store=container.store, clock=container.clock, settings=container.settings
    )

    result = _close_with_bars(container, position, service)

    assert result.status == "CLOSED" and result.trade_log is not None
    trade = result.trade_log
    assert trade["r_multiple"] is None
    assert trade["data_quality"]["status"] == "PARTIAL"
    assert any("R multiple not measured" in reason for reason in trade["data_quality"]["reasons"])
    assert "DATA_PARTIAL" in trade["outcome_tags"]


def test_a_bar_containing_both_stop_and_target_is_an_ambiguous_bar(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    target = Decimal(position["avg_entry_price"]) + Decimal("2000")
    _rewrite_payload(container, position_id, target_prices=[str(target)])
    period, version = _bars_series(container)
    stop = Decimal(position["stop_price"])
    _seed_bar(
        container,
        close=stop,
        bar_end=NOW,
        period=period,
        metric_version=version,
        run_id="ambiguous-bar",
        low=stop - Decimal("100"),
        high=target + Decimal("100"),
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

    result = service.monitor(position_id)

    assert result.status == "CLOSED" and result.trade_log is not None
    trade = result.trade_log
    assert trade["data_quality"]["status"] == "PARTIAL"
    assert any("AMBIGUOUS_BAR" in reason for reason in trade["data_quality"]["reasons"])
