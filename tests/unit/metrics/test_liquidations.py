"""Unit tests for the live-only, since-deployment liquidation accumulator."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.application.liquidation_recorder import recorded_liquidation_recorder
from oft.clock import FixedClock
from oft.domain.metrics.liquidations import (
    LIMITATIONS,
    LiquidationEvent,
    LiquidationRecorder,
    compute_liquidations,
    load_liquidation_events,
)

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _event(
    seq: int,
    side: str,
    *,
    price: str = "96000.00",
    average_price: str = "96000.00",
    quantity: str = "0.50",
    minute: int = 1,
    second: int = 5,
) -> LiquidationEvent:
    received = NOW + timedelta(minutes=minute, seconds=second)
    return LiquidationEvent(
        received_at=received,
        trade_time=received - timedelta(milliseconds=500),
        symbol="BTCUSDT",
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        average_price=Decimal(average_price),
        quantity=Decimal(quantity),
        source_seq=seq,
    )


def test_sell_liquidates_a_long_and_buy_liquidates_a_short() -> None:
    events = (
        _event(1, "SELL", quantity="0.50"),
        _event(2, "BUY", quantity="1.00", minute=2),
        _event(3, "SELL", quantity="0.25", minute=2, second=30),
    )

    result = compute_liquidations(events, symbol="BTCUSDT", coverage_start=NOW)

    assert result.coverage == "SINCE_DEPLOYMENT"
    assert result.total_long_notional == Decimal("72000")
    assert result.total_short_notional == Decimal("96000")
    assert result.count == 3
    assert result.coverage_start == NOW
    assert result.coverage_end == NOW + timedelta(minutes=2, seconds=30)


def test_buckets_split_long_short_notional_and_count() -> None:
    events = (
        _event(1, "SELL", quantity="0.50", minute=1, second=5),
        _event(2, "SELL", quantity="0.25", minute=1, second=30),
        _event(3, "BUY", quantity="1.00", minute=2, second=5),
    )

    result = compute_liquidations(events, symbol="BTCUSDT", coverage_start=NOW)

    assert [(bucket.bucket_start.minute, bucket.count) for bucket in result.buckets] == [
        (1, 2),
        (2, 1),
    ]
    first, second = result.buckets
    assert first.bucket_end - first.bucket_start == timedelta(seconds=60)
    assert first.long_notional == Decimal("72000")
    assert first.short_notional == Decimal("0")
    assert second.long_notional == Decimal("0")
    assert second.short_notional == Decimal("96000")


def test_coverage_is_none_before_any_recording() -> None:
    recorder = LiquidationRecorder(clock=FixedClock(NOW))

    result = recorder.summary()

    assert result.coverage == "NONE"
    assert result.coverage_start is None
    assert result.coverage_end is None
    assert result.count == 0
    assert result.total_long_notional == Decimal("0")
    assert result.total_short_notional == Decimal("0")
    assert result.limitations == LIMITATIONS


def test_start_sets_the_deployment_time_and_recording_auto_starts() -> None:
    recorder = LiquidationRecorder(clock=FixedClock(NOW))
    assert recorder.start() == NOW
    started = recorder.summary()
    assert started.coverage == "SINCE_DEPLOYMENT"
    assert started.coverage_start == NOW
    assert started.coverage_end is None

    auto = LiquidationRecorder(clock=FixedClock(NOW))
    assert auto.record(_event(1, "SELL")) is True
    summary = auto.summary()
    assert summary.coverage == "SINCE_DEPLOYMENT"
    assert summary.coverage_start == NOW
    assert summary.coverage_end == NOW + timedelta(minutes=1, seconds=5)


def test_duplicate_source_sequence_is_ignored() -> None:
    recorder = LiquidationRecorder(clock=FixedClock(NOW))
    event = _event(1, "SELL")

    assert recorder.record(event) is True
    assert recorder.record(event) is False
    assert recorder.summary().count == 1


def test_notional_uses_average_price_when_available_else_price() -> None:
    with_average = _event(1, "SELL", price="100.00", average_price="105.00", quantity="2")
    without_average = _event(2, "BUY", price="100.00", average_price="0", quantity="2")

    result = compute_liquidations(
        (with_average, without_average), symbol="BTCUSDT", coverage_start=NOW
    )

    assert result.total_long_notional == Decimal("210")
    assert result.total_short_notional == Decimal("200")


def test_summary_window_filter_is_half_open() -> None:
    events = (
        _event(1, "SELL", quantity="0.50", minute=1, second=30),
        _event(2, "SELL", quantity="0.25", minute=2, second=5),
    )
    recorder = LiquidationRecorder(clock=FixedClock(NOW))
    for event in events:
        recorder.record(event)

    result = recorder.summary(
        window_start=NOW + timedelta(minutes=2),
        window_end=NOW + timedelta(minutes=3),
    )

    assert result.count == 1
    assert result.total_long_notional == Decimal("24000")
    assert result.coverage_start == NOW


def test_limitations_always_state_the_feed_and_backfill_limits() -> None:
    for result in (
        compute_liquidations((), symbol="BTCUSDT", coverage_start=None),
        compute_liquidations((_event(1, "SELL"),), symbol="BTCUSDT", coverage_start=NOW),
    ):
        text = " ".join(result.limitations)
        assert "largest liquidation per symbol per second" in text
        assert "No historical USD-M liquidation backfill exists" in text
        assert "since deployment" in text.lower() or "starts at deployment" in text


def test_validation_rejects_invalid_events_and_bad_arguments() -> None:
    with pytest.raises(ValueError, match="side"):
        LiquidationEvent(
            received_at=NOW,
            trade_time=NOW,
            symbol="BTCUSDT",
            side="HOLD",  # type: ignore[arg-type]
            price=Decimal("1"),
            average_price=Decimal("1"),
            quantity=Decimal("1"),
        )
    with pytest.raises(ValueError, match="timezone"):
        LiquidationEvent(
            received_at=datetime(2026, 10, 1, 0, 0),
            trade_time=NOW,
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("1"),
            average_price=Decimal("1"),
            quantity=Decimal("1"),
        )
    with pytest.raises(ValueError, match="quantity"):
        _event(1, "SELL", quantity="0")

    with pytest.raises(ValueError, match="bucket"):
        compute_liquidations((), symbol="BTCUSDT", coverage_start=NOW, bucket_seconds=0)

    with pytest.raises(ValueError, match="coverage_start"):
        compute_liquidations((_event(1, "SELL"),), symbol="BTCUSDT", coverage_start=None)

    with pytest.raises(ValueError, match="symbol"):
        compute_liquidations((_event(1, "SELL"),), symbol="ETHUSDT", coverage_start=NOW)


def test_jsonl_loader_accepts_exact_string_decimals(tmp_path: Path) -> None:
    path = tmp_path / "liquidations.jsonl"
    row = {
        "received_at": "2026-10-01T00:01:05.000Z",
        "trade_time": "2026-10-01T00:01:04.500Z",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "price": "96000.00",
        "average_price": "96000.00",
        "quantity": "0.50",
        "source_seq": 1,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    events = load_liquidation_events(path)
    recorder = LiquidationRecorder.from_jsonl(path, clock=FixedClock(NOW))

    assert len(events) == 1
    assert events[0].notional == Decimal("48000")
    assert recorder.summary().count == 1
    assert recorder.summary().coverage_start == NOW


def test_jsonl_loader_rejects_binary_floats_and_malformed_rows(tmp_path: Path) -> None:
    path = tmp_path / "liquidations.jsonl"
    path.write_text(
        '{"received_at":"2026-10-01T00:01:05Z","trade_time":"2026-10-01T00:01:04Z",'
        '"symbol":"BTCUSDT","side":"SELL","price":96000.0,'
        '"average_price":"96000.00","quantity":"0.5"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="string"):
        load_liquidation_events(path)

    path.write_text('{"side":"SELL"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_liquidation_events(path)


def test_application_facade_opens_a_recorded_recorder(tmp_path: Path) -> None:
    path = tmp_path / "liquidations.jsonl"
    path.write_text(
        json.dumps(
            {
                "received_at": "2026-10-01T00:01:05.000Z",
                "trade_time": "2026-10-01T00:01:04.500Z",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "price": "96000.00",
                "average_price": "96000.00",
                "quantity": "0.50",
                "source_seq": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    recorder = recorded_liquidation_recorder(path, clock=FixedClock(NOW))
    summary = recorder.summary()

    assert summary.coverage == "SINCE_DEPLOYMENT"
    assert summary.coverage_start == NOW
    assert summary.count == 1
    assert summary.total_long_notional == Decimal("48000")
