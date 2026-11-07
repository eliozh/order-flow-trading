"""Dedupe and quarantine tests for duplicate/out-of-order stream events."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from oft.application.ingestion_supervisor import (
    DedupeGuard,
    DedupVerdict,
    QuarantineWriter,
)
from oft.domain.events import AggTrade, KlineEvent, MarkPriceEvent

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
STREAM = "btcusdt@aggTrade"


def _trade(agg_trade_id: int, *, event_time: datetime = NOW) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=agg_trade_id,
        price=Decimal("35000.00"),
        quantity=Decimal("0.100"),
        first_trade_id=agg_trade_id,
        last_trade_id=agg_trade_id,
        transact_time=event_time,
        is_buyer_maker=False,
    )


def _kline(*, event_time: datetime = NOW, close_time: datetime = NOW) -> KlineEvent:
    return KlineEvent(
        symbol="BTCUSDT",
        event_time=event_time,
        open_time=NOW,
        close_time=close_time,
        interval="1m",
        open=Decimal("35000.00"),
        high=Decimal("35010.00"),
        low=Decimal("34990.00"),
        close=Decimal("35005.00"),
        volume=Decimal("1.5"),
        quote_volume=Decimal("52500.00"),
        trade_count=42,
        taker_buy_volume=Decimal("0.75"),
        taker_buy_quote_volume=Decimal("26250.00"),
        is_closed=True,
    )


def _mark(*, event_time: datetime = NOW) -> MarkPriceEvent:
    return MarkPriceEvent(
        symbol="BTCUSDT",
        event_time=event_time,
        mark_price=Decimal("35000.50"),
        index_price=Decimal("34999.00"),
        funding_rate=Decimal("0.0001"),
        next_funding_time=datetime(2026, 11, 6, 16, 0, tzinfo=UTC),
    )


def test_duplicate_agg_trade_id_is_dropped() -> None:
    guard = DedupeGuard()

    assert guard.classify(_trade(10), STREAM) is DedupVerdict.NEW
    assert guard.classify(_trade(10), STREAM) is DedupVerdict.DUPLICATE
    assert guard.last_event_id(STREAM) == 10


def test_out_of_order_agg_trade_id_is_quarantined() -> None:
    guard = DedupeGuard()
    guard.classify(_trade(10), STREAM)

    assert guard.classify(_trade(9), STREAM) is DedupVerdict.OUT_OF_ORDER
    assert guard.last_event_id(STREAM) == 10


def test_lower_event_time_for_mark_price_is_out_of_order() -> None:
    guard = DedupeGuard()
    guard.classify(_mark(event_time=NOW), "btcusdt@markPrice@1s")

    verdict = guard.classify(
        _mark(event_time=datetime(2026, 11, 6, 11, 59, tzinfo=UTC)), "btcusdt@markPrice@1s"
    )

    assert verdict is DedupVerdict.OUT_OF_ORDER


def test_duplicate_kline_event_time_is_dropped() -> None:
    guard = DedupeGuard()
    guard.classify(_kline(), "btcusdt@kline_1m")

    assert guard.classify(_kline(), "btcusdt@kline_1m") is DedupVerdict.DUPLICATE


def test_quarantine_writer_records_reason_and_exports_counter(tmp_path: Path) -> None:
    writer = QuarantineWriter(tmp_path / "quarantine.jsonl")
    writer.write(_trade(9), stream=STREAM, reason="OUT_OF_ORDER")
    writer.write(_trade(10), stream=STREAM, reason="DUPLICATE")

    lines = (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert writer.counters["out_of_order"] == 1
    assert writer.counters["duplicates"] == 1
    assert "OUT_OF_ORDER" in lines[0]
    assert "DUPLICATE" in lines[1]
