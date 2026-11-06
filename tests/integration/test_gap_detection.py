"""Gap detection: expected daily files/bars vs present data marks PARTIAL."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

from oft.adapters.archive import FixtureArchiveSource
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.application.gap_detection import detect_backfill_gaps, detect_daily_gaps
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.data_quality import DataQuality, GapSeverity

ARCHIVE = Path(__file__).resolve().parents[1] / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _backfill(settings: Settings) -> None:
    store = SqliteStore(settings)
    store.initialize()
    parquet = ParquetStore(settings)
    parquet.ensure_layout()
    service = BackfillService(
        settings=settings,
        archive=FixtureArchiveSource(ARCHIVE),
        parquet=parquet,
        store=store,
        clock=FixedClock(NOW),
    )
    result = service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert result.run.status == "COMPLETE"


def test_complete_week_has_no_gaps(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _backfill(settings)
    store = SqliteStore(settings)

    result = detect_backfill_gaps(
        settings,
        store,
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=START,
        end=END,
        clock=FixedClock(NOW),
    )

    assert result.expected_days == 7
    assert result.present_days == 7
    assert result.quality.status is DataQuality.COMPLETE
    assert result.quality.gaps == ()
    assert store.list_data_gaps(symbol="BTCUSDT") == ()


def test_removing_one_daily_file_creates_exactly_one_gap_and_partial_quality(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _backfill(settings)
    store = SqliteStore(settings)
    shutil.rmtree(
        settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT" / "date=2026-10-04"
    )

    result = detect_backfill_gaps(
        settings,
        store,
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=START,
        end=END,
        clock=FixedClock(NOW),
    )

    assert result.quality.status is DataQuality.PARTIAL
    assert result.present_days == 6
    assert len(result.quality.gaps) == 1
    gap = result.quality.gaps[0]
    assert gap.start == "2026-10-04T00:00:00.000Z"
    assert gap.end == "2026-10-04T23:59:59.999Z"
    assert gap.reason == "ARCHIVE_FILE_MISSING"
    assert gap.severity is GapSeverity.CRITICAL
    assert gap.source == "BACKFILL"
    persisted = store.list_data_gaps(symbol="BTCUSDT")
    assert len(persisted) == 1
    assert persisted[0].gap_id == gap.gap_id


def test_gap_detection_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _backfill(settings)
    store = SqliteStore(settings)
    shutil.rmtree(
        settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT" / "date=2026-10-04"
    )

    for _ in range(2):
        detect_backfill_gaps(
            settings,
            store,
            symbol="BTCUSDT",
            data_type="aggTrades",
            start=START,
            end=END,
            clock=FixedClock(NOW),
        )

    assert len(store.list_data_gaps(symbol="BTCUSDT")) == 1


def test_bar_shortfall_marks_the_window_partial(tmp_path: Path) -> None:
    present = tuple(date(2026, 10, day) for day in range(1, 8))
    bars = {day: 3 for day in present}
    bars[date(2026, 10, 2)] = 1

    result = detect_daily_gaps(
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=START,
        end=END,
        present_dates=present,
        clock=FixedClock(NOW),
        expected_bars_per_day=3,
        bars_by_date=bars,
    )

    assert result.quality.status is DataQuality.PARTIAL
    assert len(result.quality.gaps) == 1
    gap = result.quality.gaps[0]
    assert gap.reason == "INSUFFICIENT_BARS"
    assert gap.severity is GapSeverity.WARNING
    assert "2026-10-02" in result.quality.reasons[0]
