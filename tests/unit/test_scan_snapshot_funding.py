"""Snapshot funding is point-in-time, never the current rate backfilled."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from oft.application.scan_service import _snapshot_bars

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _bar(offset_hours: int) -> SimpleNamespace:
    start = START + timedelta(hours=offset_hours)
    return SimpleNamespace(
        bar_start=start,
        bar_end=start + timedelta(hours=4),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        quote_volume=Decimal("100"),
        trade_count=1,
        taker_buy_volume=Decimal("1"),
    )


def test_snapshot_uses_the_funding_round_at_the_bar_time() -> None:
    funding_history = (SimpleNamespace(ts=START + timedelta(hours=8), rate=Decimal("0.0002")),)
    document = SimpleNamespace(
        cvd=SimpleNamespace(
            points=tuple(
                SimpleNamespace(
                    bar_start=_bar(hour).bar_start,
                    buy_volume=Decimal("1"),
                    sell_volume=Decimal("0"),
                    delta=Decimal("1"),
                    cvd=Decimal("1"),
                )
                for hour in (0, 8)
            )
        ),
        open_interest=None,
        funding=SimpleNamespace(history=funding_history),
    )

    bars = _snapshot_bars((_bar(0), _bar(8)), document)

    assert bars[0]["funding_rate"] is None
    assert bars[1]["funding_rate"] == "0.0002"
