"""Unit tests: UTC-aligned aggTrade bars with exact decimals and no lookahead."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from oft.domain.events import AggTrade
from oft.domain.resampling import (
    BAR_PERIODS,
    bar_metric_version,
    bar_start_for,
    bars_input_hash,
    expected_bar_starts,
    resample_agg_trades,
)

ROOT = Path(__file__).resolve().parents[2]


def _trade(
    trade_id: int,
    *,
    price: str = "100.00",
    quantity: str = "1.0",
    instant: datetime,
    maker: bool = False,
    symbol: str = "BTCUSDT",
) -> AggTrade:
    return AggTrade(
        symbol=symbol,
        agg_trade_id=trade_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=trade_id,
        last_trade_id=trade_id,
        transact_time=instant,
        is_buyer_maker=maker,
    )


DAY_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("instant", "period", "expected"),
    [
        (datetime(2026, 10, 1, 5, 59, tzinfo=UTC), "4h", datetime(2026, 10, 1, 4, tzinfo=UTC)),
        (datetime(2026, 10, 1, 20, 0, tzinfo=UTC), "4h", datetime(2026, 10, 1, 20, tzinfo=UTC)),
        (
            datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC),
            "4h",
            datetime(2026, 10, 1, 20, tzinfo=UTC),
        ),
        (datetime(2026, 10, 1, 7, 59, tzinfo=UTC), "8h", datetime(2026, 10, 1, 0, tzinfo=UTC)),
        (datetime(2026, 10, 1, 8, 0, tzinfo=UTC), "8h", datetime(2026, 10, 1, 8, tzinfo=UTC)),
        (datetime(2026, 10, 1, 15, 59, tzinfo=UTC), "8h", datetime(2026, 10, 1, 8, tzinfo=UTC)),
        (datetime(2026, 10, 1, 16, 0, tzinfo=UTC), "8h", datetime(2026, 10, 1, 16, tzinfo=UTC)),
        (datetime(2026, 10, 1, 23, 59, tzinfo=UTC), "1d", datetime(2026, 10, 1, 0, tzinfo=UTC)),
    ],
)
def test_bar_start_floors_to_utc_epoch_boundaries(
    instant: datetime, period: str, expected: datetime
) -> None:
    assert bar_start_for(instant, period) == expected


def test_4h_and_8h_boundaries_are_the_documented_utc_slots() -> None:
    four_hour = expected_bar_starts(DAY_START, DAY_END, "4h")
    eight_hour = expected_bar_starts(DAY_START, DAY_END, "8h")
    assert [slot.hour for slot in four_hour] == [0, 4, 8, 12, 16, 20]
    assert [slot.hour for slot in eight_hour] == [0, 8, 16]


def test_alignment_uses_utc_even_for_non_utc_offsets() -> None:
    # 04:30 at UTC-05 is 09:30 UTC, so the 4h slot is 08:00 UTC.
    local = datetime(2026, 10, 1, 4, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert bar_start_for(local, "4h") == datetime(2026, 10, 1, 8, tzinfo=UTC)


def test_all_seven_periods_are_supported() -> None:
    assert BAR_PERIODS == ("1m", "5m", "15m", "1h", "4h", "8h", "1d")
    assert bar_start_for(DAY_START + timedelta(minutes=90), "15m") == (
        DAY_START + timedelta(minutes=90)
    )
    with pytest.raises(ValueError, match="unsupported period"):
        bar_start_for(DAY_START, "2h")


def test_ohlcv_quote_volume_count_and_taker_buy_volume_are_exact() -> None:
    trades = (
        _trade(1, price="100.00", quantity="1.0", instant=DAY_START + timedelta(seconds=10)),
        _trade(
            2,
            price="102.00",
            quantity="0.5",
            instant=DAY_START + timedelta(seconds=20),
            maker=True,
        ),
        _trade(3, price="99.00", quantity="2.0", instant=DAY_START + timedelta(seconds=5)),
    )

    result = resample_agg_trades(
        trades,
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=1),
    )

    assert len(result.bars) == 1
    bar = result.bars[0]
    assert bar.open == Decimal("99.00")
    assert bar.high == Decimal("102.00")
    assert bar.low == Decimal("99.00")
    assert bar.close == Decimal("102.00")
    assert bar.volume == Decimal("3.5")
    assert bar.quote_volume == Decimal("349.00")
    assert bar.trade_count == 3
    assert bar.taker_buy_volume == Decimal("3.0")


def test_no_lookahead_excludes_trades_in_open_and_future_bars() -> None:
    trades = (
        _trade(1, price="100.00", instant=DAY_START + timedelta(seconds=10)),
        _trade(2, price="200.00", instant=DAY_START + timedelta(seconds=70)),
    )

    open_bar = resample_agg_trades(
        trades,
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=2),
        closed_at=DAY_START + timedelta(seconds=90),
    )
    assert len(open_bar.bars) == 1
    assert open_bar.bars[0].close == Decimal("100.00")
    # The 00:01 bar is still open, so it is not due and not "missing".
    assert open_bar.expected_bar_count == 1
    assert open_bar.missing_bar_starts == ()
    assert open_bar.bars[0].bar_start == DAY_START

    closed = resample_agg_trades(
        trades,
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=2),
        closed_at=DAY_START + timedelta(minutes=2),
    )
    assert [bar.close for bar in closed.bars] == [Decimal("100.00"), Decimal("200.00")]


def test_duplicate_ids_wrong_symbol_and_out_of_window_are_rejected() -> None:
    first = _trade(1, instant=DAY_START + timedelta(seconds=10))
    window = {
        "symbol": "BTCUSDT",
        "period": "1m",
        "window_start": DAY_START,
        "window_end": DAY_START + timedelta(minutes=1),
    }
    with pytest.raises(ValueError, match="duplicate agg_trade_id"):
        resample_agg_trades((first, first), **window)
    with pytest.raises(ValueError, match="does not match"):
        resample_agg_trades((_trade(2, instant=DAY_START, symbol="ETHUSDT"),), **window)
    with pytest.raises(ValueError, match="outside"):
        resample_agg_trades((_trade(3, instant=DAY_START - timedelta(seconds=1)),), **window)


def test_unaligned_windows_are_rejected() -> None:
    with pytest.raises(ValueError, match="aligned"):
        resample_agg_trades(
            (),
            symbol="BTCUSDT",
            period="5m",
            window_start=DAY_START + timedelta(seconds=30),
            window_end=DAY_START + timedelta(minutes=5),
        )


def test_invalid_source_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_mode"):
        resample_agg_trades(
            (),
            symbol="BTCUSDT",
            period="1m",
            window_start=DAY_START,
            window_end=DAY_START + timedelta(minutes=1),
            source_mode="GUESSED",  # type: ignore[arg-type]
        )


def test_missing_bars_are_absent_and_never_interpolated() -> None:
    trades = (_trade(1, instant=DAY_START + timedelta(minutes=2, seconds=10)),)

    result = resample_agg_trades(
        trades,
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=5),
    )

    assert [bar.bar_start for bar in result.bars] == [DAY_START + timedelta(minutes=2)]
    assert result.expected_bar_count == 5
    assert result.missing_bar_starts == (
        DAY_START,
        DAY_START + timedelta(minutes=1),
        DAY_START + timedelta(minutes=3),
        DAY_START + timedelta(minutes=4),
    )
    assert result.data_quality == "PARTIAL"
    assert result.quality_reasons == ("missing 4 of 5 expected bars",)


def test_empty_window_is_missing_not_zero_filled() -> None:
    result = resample_agg_trades(
        (),
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=5),
    )

    assert result.bars == ()
    assert result.data_quality == "MISSING"
    assert result.quality_reasons == ("missing all 5 expected bars",)


def test_input_manifest_hash_is_deterministic_and_order_independent() -> None:
    one = _trade(1, instant=DAY_START + timedelta(seconds=10))
    two = _trade(2, price="101.00", instant=DAY_START + timedelta(seconds=20))
    kwargs = {
        "symbol": "BTCUSDT",
        "period": "1m",
        "window_start": DAY_START,
        "window_end": DAY_START + timedelta(minutes=1),
    }
    first = bars_input_hash((one, two), **kwargs)
    second = bars_input_hash((two, one), **kwargs)

    assert first == second
    assert len(first) == 64
    changed = bars_input_hash(
        (_trade(2, price="101.01", instant=DAY_START + timedelta(seconds=20)), one), **kwargs
    )
    assert changed != first


def test_bar_metric_version_is_period_specific_and_filesystem_safe() -> None:
    one_minute = bar_metric_version("1m")
    four_hour = bar_metric_version("4h")

    assert one_minute.identifier != four_hour.identifier
    assert re.fullmatch(r"agg_bars\.v1\.[0-9a-f]{16}", one_minute.identifier)
    assert one_minute.identifier == bar_metric_version("1m").identifier
    with pytest.raises(ValueError, match="unsupported period"):
        bar_metric_version("2h")


def test_resampling_path_has_no_float_or_wall_clock() -> None:
    files = (
        ROOT / "src" / "oft" / "domain" / "resampling.py",
        ROOT / "src" / "oft" / "application" / "resampling_service.py",
    )
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in source, path
        assert "datetime.utcnow(" not in source, path
        assert "float(" not in source, path
        assert "zoneinfo" not in source, path


def test_source_modes_match_the_public_contract_enum() -> None:
    from oft.domain.resampling import SOURCE_MODES

    assert frozenset({"LIVE", "BACKFILL", "REPLAY", "DERIVED"}) == SOURCE_MODES
    default = resample_agg_trades(
        (),
        symbol="BTCUSDT",
        period="1m",
        window_start=DAY_START,
        window_end=DAY_START + timedelta(minutes=1),
    )
    assert default.source_mode == "BACKFILL"
    for mode in ("LIVE", "BACKFILL", "REPLAY", "DERIVED"):
        result = resample_agg_trades(
            (),
            symbol="BTCUSDT",
            period="1m",
            window_start=DAY_START,
            window_end=DAY_START + timedelta(minutes=1),
            source_mode=mode,  # type: ignore[arg-type]
        )
        assert result.source_mode == mode


def test_closed_at_at_or_before_window_start_returns_missing_not_an_error() -> None:
    for closed_at in (DAY_START, DAY_START - timedelta(minutes=5)):
        result = resample_agg_trades(
            (_trade(1, instant=DAY_START + timedelta(seconds=10)),),
            symbol="BTCUSDT",
            period="1m",
            window_start=DAY_START,
            window_end=DAY_START + timedelta(minutes=5),
            closed_at=closed_at,
        )
        assert result.bars == ()
        assert result.expected_bar_count == 0
        assert result.missing_bar_starts == ()
        assert result.data_quality == "MISSING"
        assert result.quality_reasons == ("closed_at is before the first fully closed bar",)
