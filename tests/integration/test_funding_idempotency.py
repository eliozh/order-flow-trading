"""Integration: funding accrues once per recorded settlement and never fabricates."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_partial_close_flow import NOW, _broker, _build_loop, _intent

from oft.adapters.parquet_store import ParquetStore
from oft.application.position_service import PositionService
from oft.clock import FixedClock
from oft.domain.resampling import Bar
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    ClosedPositionRecord,
    FillRecord,
    FundingAccrualCommit,
    FundingAccrualRecord,
    OmsConflictError,
    OrderRecord,
    PositionEventRecord,
    PositionRecord,
    ReportRecord,
    TradeLogRecord,
)
from position_support import set_position_mark


def _bars_series(container: Any) -> tuple[str, str]:
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
    bar_end: Any,
    period: str,
    metric_version: str,
    run_id: str,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> None:
    bar_start = bar_end - timedelta(hours=4)
    bar = Bar(
        symbol="BTCUSDT",
        period=period,
        bar_start=bar_start,
        bar_end=bar_end,
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
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
        input_manifest_hash="funding-input",
        source_mode="BACKFILL",
        source_digest="funding-digest",
        run_id=run_id,
        computed_at=utc_timestamp(bar_end),
        data_quality="COMPLETE",
        quality_reasons=(),
    )
    container.store.upsert_manifest_entry(entry)


def _source_report(container: Any, position: dict[str, Any]) -> Any:
    trigger = container.store.get_trigger_event(position["source_trigger_id"])
    assert trigger is not None
    opportunity = container.store.get_opportunity(trigger.opportunity_id)
    assert opportunity is not None
    source = json.loads(opportunity.payload_json)["source"]
    report = container.store.get_report(str(source["report_id"]))
    assert report is not None
    return report


def _inject_funding_history(
    container: Any,
    position: dict[str, Any],
    points: list[dict[str, object]],
) -> None:
    report = _source_report(container, position)
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM reports WHERE report_id = ?", (report.report_id,)
        ).fetchone()
        snapshot = json.loads(row[0])
        snapshot.setdefault("orderflow", {})["funding"] = {
            "current_rate": "0.0001",
            "next_funding_time": utc_timestamp(NOW),
            "interval_hours": 8,
            "mark_price": "71000",
            "index_price": "71000",
            "history": points,
        }
        connection.execute(
            "UPDATE reports SET snapshot_json = ? WHERE report_id = ?",
            (json.dumps(snapshot, sort_keys=True), report.report_id),
        )
        connection.commit()


def _cash(container: Any) -> Decimal:
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None
    return Decimal(account.balance)


def _position_payload(container: Any, position_id: str) -> dict[str, Any]:
    record = container.store.get_position(position_id)
    assert record is not None
    return json.loads(record.payload_json)


def test_funding_accrues_once_and_never_double_applies_across_restart(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal("71000"),
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="funding-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )
    cash_before = _cash(container)
    size = Decimal(position["size"])
    expected_amount = (Decimal("-0.0001") * Decimal("71000") * size).quantize(Decimal("0.00000001"))

    first = service.accrue_funding(position_id)
    assert first["accrued"] == 1
    record = container.store.get_position(position_id)
    assert record is not None
    payload = json.loads(record.payload_json)
    assert Decimal(payload["funding_usd"]) == expected_amount
    assert _cash(container) - cash_before == expected_amount
    accruals = container.store.list_funding_accruals(position_id)
    assert len(accruals) == 1
    assert accruals[0].settlement_time == utc_timestamp(settlement)
    assert Decimal(accruals[0].amount_usd) == expected_amount

    second = service.accrue_funding(position_id)
    assert second["accrued"] == 0
    assert len(container.store.list_funding_accruals(position_id)) == 1
    assert _cash(container) - cash_before == expected_amount

    restarted = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=3)),
        settings=container.settings,
    )
    third = restarted.accrue_funding(position_id)
    assert third["accrued"] == 0
    assert len(container.store.list_funding_accruals(position_id)) == 1
    assert _cash(container) - cash_before == expected_amount
    events = container.store.list_position_events(position_id)
    assert [event.action for event in events] == ["FUNDING"]


def test_an_unrecorded_rate_marks_the_trade_partial_and_accrues_nothing(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": None, "interval_hours": 8}],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )
    cash_before = _cash(container)

    result = service.accrue_funding(position_id)

    assert result["accrued"] == 0
    assert any("missing a recorded rate or mark" in reason for reason in result["reasons"])
    assert container.store.list_funding_accruals(position_id) == ()
    assert _cash(container) == cash_before

    set_position_mark(
        container,
        position_id,
        position["stop_price"],
        as_of=utc_timestamp(NOW + timedelta(hours=2)),
    )
    closed = service.monitor(position_id)

    assert closed.status == "CLOSED"
    assert closed.trade_log is not None
    assert closed.trade_log["data_quality"]["status"] == "PARTIAL"
    assert any(
        "missing a recorded rate or mark" in reason
        for reason in closed.trade_log["data_quality"]["reasons"]
    )
    assert closed.trade_log["funding_usd"] == "0"
    assert "DATA_PARTIAL" in closed.trade_log["outcome_tags"]


def test_funding_is_read_from_a_report_recorded_after_entry(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal("71000"),
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="later-report-bar",
    )
    source = _source_report(container, position)
    snapshot = json.loads(source.snapshot_json)
    snapshot.setdefault("orderflow", {})["funding"] = {
        "current_rate": "0.0001",
        "next_funding_time": utc_timestamp(settlement),
        "interval_hours": 8,
        "mark_price": "71000",
        "index_price": "71000",
        "history": [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    }
    container.store.save_report(
        ReportRecord(
            report_id="RPT-FUNDING-LATER",
            scan_id=source.scan_id,
            symbol=source.symbol,
            timeframe=source.timeframe,
            generated_at=utc_timestamp(settlement),
            metrics_manifest_json=source.metrics_manifest_json,
            llm_decision_id=None,
            narrative_ref=None,
            data_quality=source.data_quality,
            snapshot_json=json.dumps(snapshot, sort_keys=True),
            metrics_json=source.metrics_json,
        )
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    result = service.accrue_funding(position_id)

    assert result["accrued"] == 1
    assert len(container.store.list_funding_accruals(position_id)) == 1


def test_funding_does_not_advance_updated_at_and_the_monitor_still_sees_a_newer_bar(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    entry = Decimal(position["avg_entry_price"])
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=entry,
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="h3-settlement-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    first = service.accrue_funding(position_id)

    assert first["accrued"] == 1
    payload = _position_payload(container, position_id)
    assert payload["updated_at"] == position["updated_at"]
    assert payload["mark_price"] == position["mark_price"]
    assert payload["version"] > position["version"]
    assert all(entry["action"] != "FUNDING" for entry in payload["management_history"])

    # A newer recorded bar touches the stop: the stale stored mark must not win.
    _seed_bar(
        container,
        close=Decimal(position["stop_price"]),
        bar_end=NOW + timedelta(hours=2),
        period=period,
        metric_version=version,
        run_id="h3-stop-bar",
    )
    closed = service.monitor(position_id)

    assert closed.status == "CLOSED"
    assert [event.action for event in container.store.list_position_events(position_id)] == [
        "FUNDING",
        "FULL_CLOSE",
    ]


def test_the_funding_report_read_is_bounded_and_keeps_the_frozen_source(
    tmp_path: Path,
) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None

    everything = container.store.list_reports_for_symbol("BTCUSDT")
    assert everything
    bounded = container.store.list_reports_for_symbol(
        "BTCUSDT", since=position["opened_at"], limit=1
    )
    # At most the single newest pre-entry report plus the bounded post-entry set.
    assert 1 <= len(bounded) <= 2
    assert bounded == tuple(
        sorted(bounded, key=lambda record: (record.generated_at, record.report_id))
    )


def test_a_close_or_mark_built_from_a_pre_accrual_payload_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    assert position is not None
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal("71000"),
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="close-race-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    stale = container.store.get_position(position_id)
    assert stale is not None
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )
    assert service.accrue_funding(position_id)["accrued"] == 1
    current = container.store.get_position(position_id)
    assert current is not None
    assert current.payload_json != stale.payload_json

    # The pre-close mark persist must not clobber the concurrent accrual.
    assert (
        container.store.update_open_position_payload(
            position_id,
            payload_json=stale.payload_json,
            expected_payload_json=stale.payload_json,
        )
        is False
    )
    unchanged = container.store.get_position(position_id)
    assert unchanged is not None and unchanged.payload_json == current.payload_json

    # A full close built from the same stale payload must abort atomically.
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None
    close_record = ClosedPositionRecord(
        order=OrderRecord(
            order_id="ORD-RACE",
            client_order_id="CLI-RACE",
            environment=position["environment"],
            symbol=position["symbol"],
            status="FILLED",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
            idempotency_key="IDEM-RACE",
            created_at=utc_timestamp(NOW),
            updated_at=utc_timestamp(NOW),
            payload_json="{}",
            trigger_id=position["source_trigger_id"],
            decision_id="DEC-RACE",
            purpose="EXIT",
        ),
        fill=FillRecord(
            fill_id="FIL-RACE",
            order_id="ORD-RACE",
            environment=position["environment"],
            symbol=position["symbol"],
            filled_at=utc_timestamp(NOW),
            payload_json="{}",
        ),
        position=PositionRecord(
            position_id=position_id,
            account_id=position["account_id"],
            environment=position["environment"],
            symbol=position["symbol"],
            state="CLOSED",
            opened_at=position["opened_at"],
            payload_json=stale.payload_json,
            expected_payload_json=stale.payload_json,
        ),
        account=account,
        balance_delta="0",
        event=PositionEventRecord(
            position_id=position_id,
            seq=None,
            at=utc_timestamp(NOW),
            action="FULL_CLOSE",
            order_id="ORD-RACE",
            note="racing close",
        ),
        trade_log=TradeLogRecord(
            trade_id="TRD-RACE",
            position_id=position_id,
            account_id=position["account_id"],
            environment=position["environment"],
            symbol=position["symbol"],
            side=position["side"],
            opened_at=position["opened_at"],
            closed_at=utc_timestamp(NOW),
            net_pnl_usd="0",
            idempotency_key="IDEM-RACE",
            payload_json="{}",
            order_id="ORD-RACE",
            fill_id="FIL-RACE",
        ),
    )

    with pytest.raises(OmsConflictError) as conflict:
        container.store.save_close(close_record)

    assert conflict.value.constraint == "position_version"
    after = container.store.get_position(position_id)
    assert after is not None and after.state == "OPEN"
    assert after.payload_json == current.payload_json
    assert container.store.list_trade_logs(limit=None) == ()
    assert len(container.store.list_funding_accruals(position_id)) == 1


def test_a_funding_update_built_from_a_stale_payload_is_rejected(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal("71000"),
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="stale-guard-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    stale = container.store.get_position(position_id)
    assert stale is not None
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )
    assert service.accrue_funding(position_id)["accrued"] == 1
    account = container.store.get_paper_account("ACC-PAPER")
    assert account is not None

    with pytest.raises(OmsConflictError):
        container.store.save_funding_accrual(
            FundingAccrualCommit(
                accrual=FundingAccrualRecord(
                    accrual_id="FND-STALE",
                    position_id=position_id,
                    settlement_time="2026-11-14T23:00:00.000Z",
                    rate="0.0001",
                    mark_price="71000",
                    amount_usd="0",
                    created_at=utc_timestamp(NOW),
                ),
                position=PositionRecord(
                    position_id=position_id,
                    account_id=position["account_id"],
                    environment=position["environment"],
                    symbol=position["symbol"],
                    state="OPEN",
                    opened_at=position["opened_at"],
                    payload_json=stale.payload_json,
                    expected_payload_json=stale.payload_json,
                ),
                account=account,
                balance_delta="0",
                event=PositionEventRecord(
                    position_id=position_id,
                    seq=None,
                    at=utc_timestamp(NOW),
                    action="FUNDING",
                    order_id=None,
                    note="stale",
                ),
            )
        )
    assert len(container.store.list_funding_accruals(position_id)) == 1


class _ConflictOnceStore:
    """Delegate to a real store but fail the first funding commit."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._raised = False
        self.funding_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def save_funding_accrual(self, record: FundingAccrualCommit) -> bool:
        self.funding_calls += 1
        if not self._raised:
            self._raised = True
            raise OmsConflictError("position_version")
        return bool(self._store.save_funding_accrual(record))


def test_a_concurrent_funding_update_is_retried(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=Decimal("71000"),
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="retry-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    store = _ConflictOnceStore(container.store)
    service = PositionService(
        store=store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    result = service.accrue_funding(position_id)

    assert result["accrued"] == 1
    assert store.funding_calls == 2
    assert len(container.store.list_funding_accruals(position_id)) == 1


def test_each_settlement_is_price_at_the_size_in_force(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    size = Decimal(position["size"])
    half = size / Decimal("2")
    entry = Decimal(position["avg_entry_price"])
    partial_at = NOW + timedelta(minutes=30)
    before = NOW + timedelta(minutes=20)
    after = NOW + timedelta(minutes=40)
    period, version = _bars_series(container)
    for close, bar_end, run_id in (
        (Decimal("71000"), before, "m7-before-bar"),
        (Decimal("72000"), after, "m7-after-bar"),
    ):
        _seed_bar(
            container,
            close=close,
            bar_end=bar_end,
            period=period,
            metric_version=version,
            run_id=run_id,
        )
    _broker(container).submit(
        _intent(
            position,
            purpose="EXIT",
            quantity=str(half),
            idempotency_key="m7-partial",
            risk_verdict_id=loop["order"]["risk_verdict_id"],
        ),
        mark_price=entry,
        mark_as_of=position["updated_at"],
        now=utc_timestamp(partial_at),
    )
    _inject_funding_history(
        container,
        position,
        [
            {"ts": utc_timestamp(before), "rate": "0.0001", "interval_hours": 8},
            {"ts": utc_timestamp(after), "rate": "0.0001", "interval_hours": 8},
        ],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    result = service.accrue_funding(position_id)

    assert result["accrued"] == 2
    amounts = {
        accrual.settlement_time: Decimal(accrual.amount_usd)
        for accrual in container.store.list_funding_accruals(position_id)
    }
    rate = Decimal("0.0001")
    assert amounts[utc_timestamp(before)] == (rate * Decimal("71000") * size * -1).quantize(
        Decimal("0.00000001")
    )
    assert amounts[utc_timestamp(after)] == (rate * Decimal("72000") * (size - half) * -1).quantize(
        Decimal("0.00000001")
    )


def test_a_full_close_includes_a_non_zero_funding_term(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    entry = Decimal(position["avg_entry_price"])
    size = Decimal(position["size"])
    period, version = _bars_series(container)
    _seed_bar(
        container,
        close=entry,
        bar_end=settlement,
        period=period,
        metric_version=version,
        run_id="close-funding-bar",
    )
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "0.0001", "interval_hours": 8}],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )
    assert service.accrue_funding(position_id)["accrued"] == 1
    funding = (Decimal("-1") * Decimal("0.0001") * entry * size).quantize(Decimal("0.00000001"))
    set_position_mark(
        container,
        position_id,
        position["stop_price"],
        as_of=utc_timestamp(NOW + timedelta(hours=2)),
    )

    closed = service.monitor(position_id)

    assert closed.status == "CLOSED" and closed.trade_log is not None
    trade = closed.trade_log
    assert Decimal(trade["funding_usd"]) == funding != 0
    assert Decimal(trade["net_pnl_usd"]) == (
        Decimal(trade["gross_pnl_usd"]) - Decimal(trade["fees_usd"]) + Decimal(trade["funding_usd"])
    ).quantize(Decimal("0.00000001"))


def test_an_implausible_recorded_rate_is_named_and_never_accrued(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    settlement = NOW + timedelta(hours=1)
    _inject_funding_history(
        container,
        position,
        [{"ts": utc_timestamp(settlement), "rate": "1e100", "interval_hours": 8}],
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    result = service.accrue_funding(position_id)

    assert result["accrued"] == 0
    assert any("implausible rate" in reason for reason in result["reasons"])
    assert container.store.list_funding_accruals(position_id) == ()


def test_a_corrupt_funding_source_marks_the_trade_partial(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    container = loop["container"]
    position = loop["position"]
    position_id = position["position_id"]
    report = _source_report(container, position)
    database = container.settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE reports SET snapshot_json = ? WHERE report_id = ?",
            ("{not valid json", report.report_id),
        )
        connection.commit()
    set_position_mark(
        container,
        position_id,
        position["stop_price"],
        as_of=utc_timestamp(NOW + timedelta(hours=2)),
    )
    service = PositionService(
        store=container.store,
        clock=FixedClock(NOW + timedelta(hours=2)),
        settings=container.settings,
    )

    closed = service.monitor(position_id)

    assert closed.status == "CLOSED" and closed.trade_log is not None
    trade = closed.trade_log
    assert trade["data_quality"]["status"] == "PARTIAL"
    assert any("funding source is corrupt" in reason for reason in trade["data_quality"]["reasons"])
    assert "DATA_PARTIAL" in trade["outcome_tags"]
