"""Integration: resample normalized aggTrades into versioned, immutable bar parts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oft.adapters.parquet_store import ParquetStore, bar_parts, read_bars
from oft.adapters.sqlite_store import SqliteStore
from oft.application.freshness import (
    BAR_4H_STREAM,
    ClockSkewDetector,
    FreshnessGuard,
)
from oft.application.resampling_service import (
    BarResampleRequest,
    ResamplingService,
)
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.events import AggTrade

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "golden"

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
DAY_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
PERIODS = ("1m", "5m", "15m", "1h", "4h", "8h", "1d")


def _trade(
    trade_id: int,
    *,
    price: str,
    quantity: str,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    maker: bool = False,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        agg_trade_id=trade_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=trade_id,
        last_trade_id=trade_id,
        transact_time=DAY_START + timedelta(hours=hour, minutes=minute, seconds=second),
        is_buyer_maker=maker,
    )


def _oneday_trades() -> tuple[AggTrade, ...]:
    """A deterministic one-day fixture; some periods intentionally have gaps."""
    return (
        _trade(1, price="100.00", quantity="1.0", second=30),
        _trade(2, price="101.00", quantity="0.5", second=45, maker=True),
        _trade(3, price="99.00", quantity="2.0", minute=1, second=10),
        _trade(4, price="102.00", quantity="1.5", hour=4),
        _trade(5, price="98.00", quantity="0.25", hour=6, minute=30, second=20, maker=True),
        _trade(6, price="103.00", quantity="1.0", hour=8, maker=True),
        _trade(7, price="104.00", quantity="2.0", hour=12),
        _trade(8, price="105.00", quantity="0.5", hour=16),
        _trade(9, price="106.00", quantity="1.0", hour=20, maker=True),
        _trade(10, price="107.00", quantity="0.75", hour=23, minute=59, second=59),
    )


class _Harness:
    def __init__(self, tmp_path: Path, *, now: datetime = NOW) -> None:
        self.settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
        self.store = SqliteStore(self.settings)
        self.store.initialize()
        self.parquet = ParquetStore(self.settings)
        self.parquet.ensure_layout()
        self.clock = FixedClock(now)
        self.service = ResamplingService(
            settings=self.settings,
            store=self.store,
            parquet=self.parquet,
            clock=self.clock,
        )


def _request(
    period: str,
    *,
    start: datetime = DAY_START,
    end: datetime = DAY_END,
    source_mode: str = "BACKFILL",
) -> BarResampleRequest:
    return BarResampleRequest(
        symbol="BTCUSDT",
        period=period,
        window_start=start,
        window_end=end,
        source_mode=source_mode,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("period", PERIODS)
def test_to_contract_output_matches_the_golden_snapshots(tmp_path: Path, period: str) -> None:
    """Snapshot check: implementation output must equal the committed goldens.

    These goldens are independently recomputed from the raw trades by
    ``test_goldens_match_an_independent_stdlib_recomputation`` below, so this
    test only detects unintended drift in the implementation's serialization.
    """
    harness = _Harness(tmp_path)
    golden = json.loads((GOLDEN / f"bars_btcusdt_{period}.json").read_text(encoding="utf-8"))

    run = harness.service.compute(_request(period), trades=_oneday_trades())

    assert run.result.to_contract() == golden


def test_goldens_have_no_binary_floats() -> None:
    for period in PERIODS:
        document = json.loads((GOLDEN / f"bars_btcusdt_{period}.json").read_text(encoding="utf-8"))
        for bar in document["bars"]:
            for key in ("open", "high", "low", "close", "volume", "quote_volume"):
                assert isinstance(bar[key], str), (period, key)
            assert isinstance(bar["taker_buy_volume"], str)
            assert isinstance(bar["trade_count"], int)


def test_persisted_bars_follow_adr_0002_layout_and_carry_provenance(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    window_end = DAY_START + timedelta(minutes=4)
    trades = tuple(
        _trade(index + 1, price=f"{100 + index}.00", quantity="1.0", minute=index)
        for index in range(4)
    )
    run = harness.service.compute(_request("1m", end=window_end), trades=trades)

    entry = harness.service.persist(run)
    version = run.result.metric_version

    assert Path(entry.path).is_file()
    assert f"bars/symbol=BTCUSDT/period=1m/metric_version={version}/date=2026-10-01/" in entry.path
    assert entry.data_type == "bars"
    assert entry.source == f"bars:{version}:{run.run_id}"
    assert entry.metric_version == version
    assert entry.input_manifest_hash == run.result.input_manifest_hash
    assert entry.rows == 4

    rows = read_bars(
        harness.settings,
        symbol="BTCUSDT",
        period="1m",
        metric_version=version,
        run_id=run.run_id,
    )
    assert len(rows) == 4
    assert {row["source_mode"] for row in rows} == {"BACKFILL"}
    assert {row["input_manifest_hash"] for row in rows} == {run.result.input_manifest_hash}
    assert {row["source_digest"] for row in rows} == {run.source_digest}
    assert {row["metric_version"] for row in rows} == {version}
    assert rows[0]["open"] == "100.00"
    assert rows[0]["trade_count"] == 1

    manifest = [item for item in harness.store.list_manifest() if item.path == entry.path]
    assert len(manifest) == 1
    assert bar_parts(harness.settings, symbol="BTCUSDT", period="1m", metric_version=version) == (
        Path(entry.path),
    )


def test_persisting_the_same_run_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    run = harness.service.compute(_request("1d"), trades=_oneday_trades())

    first = harness.service.persist(run)
    first_bytes = Path(first.path).read_bytes()
    second = harness.service.persist(run)

    assert second.path == first.path
    assert Path(second.path).read_bytes() == first_bytes


def _minute_trades(count: int, *, first_id: int = 1) -> tuple[AggTrade, ...]:
    return tuple(
        _trade(first_id + minute, price=f"{100 + minute}.00", quantity="1.0", minute=minute)
        for minute in range(count)
    )


def test_resample_reads_published_parts_and_records_a_source_digest(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    entries = harness.parquet.write_agg_trades_batch(
        _minute_trades(10), symbol="BTCUSDT", source="fixture:events"
    )
    harness.store.upsert_manifest_entries(entries)
    request = _request("1m", end=DAY_START + timedelta(minutes=10))

    run = harness.service.compute(request)

    assert len(run.result.bars) == 10
    expected_digest = hashlib.sha256(
        "\n".join(
            f"{entry.path}|{entry.checksum}"
            for entry in sorted(entries, key=lambda entry: entry.path)
        ).encode("utf-8")
    ).hexdigest()
    assert run.source_digest == expected_digest
    assert run.source_digest != run.result.input_manifest_hash

    persisted = harness.service.resample_and_persist(request)
    assert Path(persisted.entry.path).is_file()
    rows = read_bars(
        harness.settings,
        symbol="BTCUSDT",
        period="1m",
        metric_version=persisted.run.result.metric_version,
        run_id=persisted.run.run_id,
    )
    assert {row["source_digest"] for row in rows} == {expected_digest}
    assert [row.path for row in harness.store.list_manifest()].count(persisted.entry.path) == 1


def test_late_event_recomputes_only_the_last_n_bars_into_a_new_versioned_run(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    window_end = DAY_START + timedelta(minutes=10)
    base = _minute_trades(10)
    request = _request("1m", end=window_end)

    initial = harness.service.compute(request, trades=base)
    initial_entry = harness.service.persist(initial)
    initial_bytes = Path(initial_entry.path).read_bytes()

    late = _trade(999, price="999.00", quantity="2.0", minute=9, second=30)
    late_run = harness.service.recompute_late(request, late_trades=(late,), trades=base, last_n=2)

    assert late_run.last_n == 2
    assert late_run.recomputed_from == DAY_START + timedelta(minutes=8)
    assert [bar.bar_start for bar in late_run.result.bars] == [
        DAY_START + timedelta(minutes=8),
        DAY_START + timedelta(minutes=9),
    ]
    assert late_run.result.bars[-1].close == Decimal("999.00")
    assert late_run.result.bars[-1].trade_count == 2
    assert late_run.run_id != initial.run_id

    late_entry = harness.service.persist(late_run)
    assert late_entry.path != initial_entry.path
    assert Path(initial_entry.path).read_bytes() == initial_bytes
    assert Path(late_entry.path).is_file()

    # A duplicate replay of the same late event is idempotent.
    repeated = harness.service.recompute_late(request, late_trades=(late,), trades=base, last_n=2)
    repeated_entry = harness.service.persist(repeated)
    assert repeated.run_id == late_run.run_id
    assert repeated_entry.path == late_entry.path

    rows = read_bars(
        harness.settings,
        symbol="BTCUSDT",
        period="1m",
        metric_version=late_run.result.metric_version,
        run_id=late_run.run_id,
    )
    assert [row["bar_start"] for row in rows] == [
        DAY_START + timedelta(minutes=8),
        DAY_START + timedelta(minutes=9),
    ]
    assert rows[-1]["close"] == "999.00"
    assert rows[-1]["trade_count"] == 2


def test_late_event_older_than_the_recompute_window_is_rejected(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    request = _request("1m", end=DAY_START + timedelta(minutes=10))
    base = _minute_trades(10)
    stale = _trade(999, price="999.00", quantity="1.0", second=30)

    with pytest.raises(ValueError, match="last 2 bars"):
        harness.service.recompute_late(request, late_trades=(stale,), trades=base, last_n=2)


def test_missing_bars_flow_into_the_metric_input_as_partial_or_missing(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    partial_run = harness.service.compute(
        _request("1m", end=DAY_START + timedelta(minutes=5)),
        trades=(_trade(1, price="100.00", quantity="1.0", minute=2),),
    )

    partial = partial_run.bar_metric_input()
    assert partial.data_quality == "PARTIAL"
    assert partial.quality_reasons == ("missing 4 of 5 expected bars",)
    assert partial.gaps
    assert {(gap.start, gap.end) for gap in partial.gaps} == {
        ("2026-10-01T00:00:00.000Z", "2026-10-01T00:02:00.000Z"),
        ("2026-10-01T00:03:00.000Z", "2026-10-01T00:05:00.000Z"),
    }
    assert [bar.bar_start for bar in partial.bars] == [DAY_START + timedelta(minutes=2)]

    missing_run = harness.service.compute(
        _request("1m", end=DAY_START + timedelta(minutes=5)), trades=()
    )
    missing = missing_run.bar_metric_input()
    assert missing.data_quality == "MISSING"
    assert missing.bars == ()
    assert missing.gaps

    complete_run = harness.service.compute(_request("4h"), trades=_oneday_trades())
    complete = complete_run.bar_metric_input()
    assert complete.data_quality == "COMPLETE"
    assert complete.gaps == ()
    assert len(complete.bars) == 6


def test_bars_are_absent_for_missing_slots_in_every_golden() -> None:
    for period, expected_present, expected_slots in (
        ("1m", 9, 1440),
        ("5m", 8, 288),
        ("15m", 8, 96),
        ("1h", 8, 24),
        ("4h", 6, 6),
        ("8h", 3, 3),
        ("1d", 1, 1),
    ):
        document = json.loads((GOLDEN / f"bars_btcusdt_{period}.json").read_text(encoding="utf-8"))
        assert document["expected_bar_count"] == expected_slots
        assert len(document["bars"]) == expected_present
        assert document["missing_bar_count"] == expected_slots - expected_present
        for bar in document["bars"]:
            assert bar["bar_start"].endswith("Z")


def test_freshness_bar_budget_and_clock_skew_guard_the_ingest_clock() -> None:
    class MutableClock:
        def __init__(self, instant: datetime) -> None:
            self.instant = instant

        def now(self) -> datetime:
            return self.instant

    clock = MutableClock(NOW)
    guard = FreshnessGuard(clock=clock)
    guard.record(BAR_4H_STREAM, NOW, closes_at=NOW)
    clock.instant = NOW + timedelta(minutes=10)
    assert guard.is_stale(BAR_4H_STREAM) is False
    clock.instant = NOW + timedelta(minutes=10, microseconds=1)
    assert guard.is_stale(BAR_4H_STREAM) is True

    clock.instant = NOW + timedelta(seconds=7)
    skew = ClockSkewDetector(clock=clock).observe(reference_now=NOW)
    assert skew.detected is True


_PERIOD_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "8h": 480, "1d": 1440}


def _independent_iso(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _independent_bars(period_minutes: int) -> list[dict[str, object]]:
    buckets: dict[datetime, list[AggTrade]] = {}
    for trade in _oneday_trades():
        offset_minutes = int((trade.transact_time - DAY_START).total_seconds() // 60)
        slot = DAY_START + timedelta(minutes=(offset_minutes // period_minutes) * period_minutes)
        buckets.setdefault(slot, []).append(trade)
    bars: list[dict[str, object]] = []
    for slot in sorted(buckets):
        group = sorted(buckets[slot], key=lambda trade: (trade.transact_time, trade.agg_trade_id))
        prices = [trade.price for trade in group]
        bars.append(
            {
                "bar_start": _independent_iso(slot),
                "bar_end": _independent_iso(slot + timedelta(minutes=period_minutes)),
                "open": format(group[0].price, "f"),
                "high": format(max(prices), "f"),
                "low": format(min(prices), "f"),
                "close": format(group[-1].price, "f"),
                "volume": format(sum((trade.quantity for trade in group), Decimal("0")), "f"),
                "quote_volume": format(
                    sum((trade.price * trade.quantity for trade in group), Decimal("0")), "f"
                ),
                "trade_count": len(group),
                "taker_buy_volume": format(
                    sum(
                        (trade.quantity for trade in group if not trade.is_buyer_maker),
                        Decimal("0"),
                    ),
                    "f",
                ),
            }
        )
    return bars


@pytest.mark.parametrize("period", PERIODS)
def test_goldens_match_an_independent_stdlib_recomputation(period: str) -> None:
    """Verify goldens against stdlib math, not against the implementation.

    This keeps the golden suite honest: if the resampler and its serializer
    drift together, the implementation snapshot test would still pass, but
    this recomputation from the raw trades would fail.
    """
    golden = json.loads((GOLDEN / f"bars_btcusdt_{period}.json").read_text(encoding="utf-8"))
    minutes = _PERIOD_MINUTES[period]
    expected_bars = _independent_bars(minutes)
    expected_slots = sum(
        1
        for offset in range(0, 24 * 60 // minutes)
        if DAY_START + timedelta(minutes=offset * minutes) < DAY_END
    )

    assert golden["source_mode"] == "BACKFILL"
    assert golden["window"] == {
        "start": _independent_iso(DAY_START),
        "end": _independent_iso(DAY_END),
    }
    assert golden["bars"] == expected_bars
    assert golden["expected_bar_count"] == expected_slots
    assert golden["missing_bar_count"] == expected_slots - len(expected_bars)
    present = {bar["bar_start"] for bar in expected_bars}
    expected_missing = [
        _independent_iso(DAY_START + timedelta(minutes=offset * minutes))
        for offset in range(0, 24 * 60 // minutes)
        if _independent_iso(DAY_START + timedelta(minutes=offset * minutes)) not in present
    ]
    assert golden["missing_bar_count"] == len(expected_missing)


@pytest.mark.parametrize("quantity", ["2.0", "0.3"])
def test_default_read_returns_the_newest_run_when_computed_at_ties(
    tmp_path: Path, quantity: str
) -> None:
    harness = _Harness(tmp_path)
    request = _request("1m", end=DAY_START + timedelta(minutes=10))
    base = _minute_trades(10)
    initial = harness.service.compute(request, trades=base)
    harness.service.persist(initial)

    late = _trade(999, price="999.31", quantity=quantity, minute=9, second=30)
    late_run = harness.service.recompute_late(request, late_trades=(late,), trades=base, last_n=2)
    harness.service.persist(late_run)

    assert late_run.computed_at == initial.computed_at
    rows = read_bars(
        harness.settings,
        symbol="BTCUSDT",
        period="1m",
        metric_version=late_run.result.metric_version,
    )
    assert {row["run_id"] for row in rows} == {late_run.run_id}
    assert rows[-1]["close"] == "999.31"


def test_recompute_late_before_any_bar_closed_returns_missing(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, now=DAY_START - timedelta(minutes=5))
    request = _request("1m", end=DAY_START + timedelta(minutes=10))
    base = _minute_trades(10)
    late = _trade(999, price="999.31", quantity="2.0", minute=9, second=30)

    run = harness.service.recompute_late(request, late_trades=(late,), trades=base, last_n=2)

    assert run.result.bars == ()
    assert run.result.expected_bar_count == 0
    assert run.result.data_quality == "MISSING"
    assert run.result.quality_reasons == ("closed_at is before the first fully closed bar",)
    assert run.last_n == 2
    assert run.recomputed_from is None
