"""One-week archive backfill: estimates, conversion, dedupe, resume, checksums."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import structlog

from oft.adapters.archive import FixtureArchiveSource
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_store import SqliteStore
from oft.application.backfill_service import BackfillService
from oft.clock import FixedClock
from oft.config import Settings
from oft.ports.archive import ChecksumMismatchError, DownloadError

ARCHIVE = Path(__file__).resolve().parents[1] / "fixtures" / "archive"
START = date(2026, 10, 1)
END = date(2026, 10, 7)
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
EXPECTED_UNIQUE_ROWS = 21  # 22 CSV rows; day 3 carries one duplicate agg_trade_id


@dataclass
class _Harness:
    settings: Settings
    store: SqliteStore
    parquet: ParquetStore
    service: BackfillService


def _harness(tmp_path: Path, archive: FixtureArchiveSource | None = None) -> _Harness:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    parquet = ParquetStore(settings)
    parquet.ensure_layout()
    service = BackfillService(
        settings=settings,
        archive=archive or FixtureArchiveSource(ARCHIVE),
        parquet=parquet,
        store=store,
        clock=FixedClock(NOW),
    )
    return _Harness(settings=settings, store=store, parquet=parquet, service=service)


def _partitions(settings: Settings) -> list[Path]:
    root = settings.data_dir / "market" / "um" / "agg_trades" / "symbol=BTCUSDT"
    return sorted(path for path in root.glob("date=*/**/*.parquet"))


def _all_rows(settings: Settings) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for part in _partitions(settings):
        rows.extend(pq.read_table(part).to_pylist())
    return rows


def test_size_estimate_precedes_any_download(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    estimate = harness.service.estimate(
        symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
    )

    assert estimate.days == 7
    assert estimate.files == 7
    assert estimate.estimated_bytes == sum(
        path.stat().st_size for path in ARCHIVE.glob("btcusdt_agg-trades_2026-10-0[1-7].zip")
    )
    assert _partitions(harness.settings) == []
    assert harness.store.list_backfill_runs() == ()


def test_one_week_converts_to_partitioned_parquet(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "COMPLETE"
    assert len(result.files) == 7
    assert all(row.status == "COMPLETE" for row in result.files)
    assert all(row.checksum_verified for row in result.files)
    assert all(row.verified_at is not None for row in result.files)
    assert all(row.bytes > 0 and row.source_url.endswith(".zip") for row in result.files)

    partitions = _partitions(harness.settings)
    assert len(partitions) == 7
    for day in range(1, 8):
        assert any(f"date=2026-10-{day:02d}" in str(path) for path in partitions)

    rows = _all_rows(harness.settings)
    assert len(rows) == EXPECTED_UNIQUE_ROWS
    assert len({row["agg_trade_id"] for row in rows}) == EXPECTED_UNIQUE_ROWS
    first = rows[0]
    assert set(first) == {
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
        "symbol",
        "source",
        "ingested_at",
        "record_hash",
    }
    assert first["symbol"] == "BTCUSDT"
    assert str(first["source"]).startswith("archive:")
    assert len(str(first["record_hash"])) == 64


def test_rerun_is_idempotent_and_does_not_duplicate_rows(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    parts_before = _partitions(harness.settings)
    rows_before = _all_rows(harness.settings)

    second = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert first.run.status == second.run.status == "COMPLETE"
    assert _partitions(harness.settings) == parts_before
    assert _all_rows(harness.settings) == rows_before
    manifest = harness.store.list_backfill_manifest(symbol="BTCUSDT")
    assert len(manifest) == 7


def test_checksum_mismatch_fails_closed_and_is_resumable(tmp_path: Path) -> None:
    copied = tmp_path / "archive"
    shutil.copytree(ARCHIVE, copied)
    checksum = copied / "btcusdt_agg-trades_2026-10-04.zip.CHECKSUM"
    good_checksum = checksum.read_text(encoding="utf-8")
    checksum.write_text(f"{'0' * 64}  btcusdt_agg-trades_2026-10-04.zip\n", encoding="utf-8")
    harness = _harness(tmp_path, FixtureArchiveSource(copied))

    with structlog.testing.capture_logs() as logs, pytest.raises(ChecksumMismatchError):
        harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    failed = harness.store.get_backfill_manifest("aggTrades/BTCUSDT/2026-10-04")
    assert failed is not None
    assert failed.status != "COMPLETE"
    assert failed.error_code == "BACKFILL_CHECKSUM_MISMATCH"
    assert not failed.checksum_verified
    assert not any("date=2026-10-04" in str(path) for path in _partitions(harness.settings))
    assert any(entry["event"] == "backfill_checksum_mismatch" for entry in logs)
    run = harness.store.latest_backfill_run()
    assert run is not None and run.status == "FAILED"

    # Restore the checksum and retry: the verified days are not redownloaded.
    checksum.write_text(good_checksum, encoding="utf-8")
    recording = _RecordingSource(copied)
    retry = _harness(tmp_path, recording)
    result = retry.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "COMPLETE"
    assert date(2026, 10, 4) in recording.opened
    assert date(2026, 10, 1) not in recording.opened
    assert len(_all_rows(retry.settings)) == EXPECTED_UNIQUE_ROWS


class _RecordingSource(FixtureArchiveSource):
    def __init__(self, fixtures_dir: Path) -> None:
        super().__init__(fixtures_dir)
        self.opened: list[date] = []

    def open(self, file):  # type: ignore[no-untyped-def]
        self.opened.append(file.date)
        return super().open(file)


class _FailingAfterThird(_RecordingSource):
    def open(self, file):  # type: ignore[no-untyped-def]
        if file.date >= date(2026, 10, 4):
            raise DownloadError(f"simulated outage for {file.date}")
        return super().open(file)


def test_partial_download_resumes_from_last_verified_file(tmp_path: Path) -> None:
    flaky = _FailingAfterThird(ARCHIVE)
    harness = _harness(tmp_path, flaky)

    partial = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert partial.run.status == "PARTIAL"
    assert partial.run.files_verified == 3
    assert partial.run.files_failed == 4
    assert len(_partitions(harness.settings)) == 3
    assert [
        row.date
        for row in harness.store.list_backfill_manifest(symbol="BTCUSDT")
        if row.status == "COMPLETE"
    ] == ["2026-10-01", "2026-10-02", "2026-10-03"]

    resumed = _RecordingSource(ARCHIVE)
    retry = _harness(tmp_path, resumed)
    final = retry.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert final.run.status == "COMPLETE"
    assert resumed.opened == [
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    ]
    assert len(_all_rows(retry.settings)) == EXPECTED_UNIQUE_ROWS


def test_mid_stream_truncation_is_a_retryable_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.client

    import oft.application.backfill_service as backfill_module

    def broken_stream(
        stream: object, path: Path, *, max_bytes: int, on_progress: object = None
    ) -> int:
        raise http.client.IncompleteRead(b"partial body")

    monkeypatch.setattr(backfill_module, "stream_to_file", broken_stream)
    harness = _harness(tmp_path)

    result = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "PARTIAL"
    assert result.run.files_failed == 7
    assert result.run.files_verified == 0
    rows = harness.store.list_backfill_manifest(symbol="BTCUSDT")
    assert len(rows) == 7
    assert all(row.status == "FAILED" and row.error_code == "DATA_UNAVAILABLE" for row in rows)
    assert _partitions(harness.settings) == []


def test_completed_backfill_persists_gap_rows_for_missing_days(tmp_path: Path) -> None:
    harness = _harness(tmp_path, _FailingAfterThird(ARCHIVE))

    result = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "PARTIAL"
    gaps = harness.store.list_data_gaps(symbol="BTCUSDT", data_type="aggTrades")
    assert {gap.start[:10] for gap in gaps} == {
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    }


def test_corrupt_existing_part_fails_closed_and_does_not_duplicate(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from oft.ports.store import StoreUnavailableError

    harness = _harness(tmp_path)
    harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    parts_before = _partitions(harness.settings)
    for row in harness.store.list_backfill_manifest(symbol="BTCUSDT"):
        harness.store.upsert_backfill_manifest(
            replace(row, status="FAILED", checksum_verified=False)
        )
    parts_before[0].write_bytes(b"not a parquet file")

    with pytest.raises(StoreUnavailableError) as excinfo:
        harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert _partitions(harness.settings) == parts_before
    failed_runs = [
        run
        for run in harness.store.list_backfill_runs()
        if run.status == "FAILED" and run.error_code == "DATA_UNAVAILABLE"
    ]
    assert len(failed_runs) == 1


def test_retry_resolves_gaps_and_reports_complete(tmp_path: Path) -> None:
    from oft.application.gap_detection import detect_backfill_gaps
    from oft.domain.data_quality import DataQuality

    partial_harness = _harness(tmp_path, _FailingAfterThird(ARCHIVE))
    partial = partial_harness.service.run(
        symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
    )
    assert partial.run.status == "PARTIAL"
    assert len(partial_harness.store.list_data_gaps(symbol="BTCUSDT")) == 4

    retry = _harness(tmp_path)
    final = retry.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert final.run.status == "COMPLETE"
    gaps = retry.store.list_data_gaps(symbol="BTCUSDT")
    assert len(gaps) == 4
    assert all(
        gap.resolved_at is not None and gap.resolution == "BACKFILL_RESOLVED" for gap in gaps
    )
    quality = detect_backfill_gaps(
        retry.settings,
        retry.store,
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=START,
        end=END,
        clock=FixedClock(NOW),
    )
    assert quality.quality.status is DataQuality.COMPLETE


def test_each_published_part_is_recorded_in_the_manifest(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    parts = {str(path) for path in _partitions(harness.settings)}
    entries = harness.store.list_manifest(symbol="BTCUSDT")
    assert {entry.path for entry in entries} == parts
    assert all(entry.checksum and entry.rows > 0 and entry.bytes > 0 for entry in entries)
    assert all(entry.source.startswith("archive:") for entry in entries)


def test_commit_failure_never_leaves_a_complete_row_without_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.ports.store import StoreUnavailableError

    harness = _harness(tmp_path)
    original = harness.store.commit_backfill_file
    calls = {"count": 0}

    def flaky(**kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise StoreUnavailableError("simulated commit failure")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.store, "commit_backfill_file", flaky)

    with pytest.raises(StoreUnavailableError):
        harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    row = harness.store.get_backfill_manifest("aggTrades/BTCUSDT/2026-10-01")
    assert row is None or row.status != "COMPLETE"
    assert not any("date=2026-10-01" in str(path) for path in _partitions(harness.settings))

    final = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    assert final.run.status == "COMPLETE"
    assert len(_partitions(harness.settings)) == 7


def test_resume_reprocesses_when_a_published_part_is_missing(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)
    part = next(path for path in _partitions(harness.settings) if "date=2026-10-01" in str(path))
    part.unlink()

    result = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "COMPLETE"
    assert any("date=2026-10-01" in str(path) for path in _partitions(harness.settings))
    assert len(_all_rows(harness.settings)) == EXPECTED_UNIQUE_ROWS


def test_lease_loss_aborts_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oft.application.backfill_service import BackfillError

    harness = _harness(tmp_path)
    monkeypatch.setattr(harness.store, "touch_backfill_run", lambda *args, **kwargs: False)

    with pytest.raises(BackfillError):
        harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    run = harness.store.latest_backfill_run()
    assert run is not None and run.status != "COMPLETE"


def test_progress_heartbeats_during_file_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        harness.store,
        "touch_backfill_run",
        lambda run_id, **kwargs: (calls.append(run_id), True)[1],
    )
    harness.service._progress_interval_seconds = 0.0

    result = harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "COMPLETE"
    assert len(calls) > 7  # in-flight chunks, not just one per file


class _StaleChecksumSource(_RecordingSource):
    def read_checksum(self, file):  # type: ignore[no-untyped-def]
        if file.date == date(2026, 10, 2):
            return f"{'a' * 64}  {file.filename}\n"
        return super().read_checksum(file)


def test_resume_revalidates_the_remote_checksum_and_reprocesses(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    stale = _harness(tmp_path, _StaleChecksumSource(ARCHIVE))
    with structlog.testing.capture_logs() as logs, pytest.raises(ChecksumMismatchError):
        stale.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    events = {entry["event"] for entry in logs}
    assert "backfill_manifest_stale" in events
    assert "backfill_checksum_mismatch" in events


def test_concurrent_second_run_does_not_change_the_first_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oft.application.backfill_service import BackfillAlreadyRunning

    harness = _harness(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = harness.service._process_file

    def slow(run, file, owner):  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=5)
        return original(run, file, owner)

    monkeypatch.setattr(harness.service, "_process_file", slow)
    results: dict[str, object] = {}

    def first_run() -> None:
        results["result"] = harness.service.run(
            symbol="BTCUSDT", data_type="aggTrades", start=START, end=END
        )

    thread = threading.Thread(target=first_run)
    thread.start()
    assert started.wait(timeout=5)

    with pytest.raises(BackfillAlreadyRunning):
        harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results["result"].run.status == "COMPLETE"  # type: ignore[union-attr]


class _OfflineSource(_RecordingSource):
    def read_checksum(self, file):  # type: ignore[no-untyped-def]
        raise DownloadError("network is offline")


def test_offline_retry_skips_verified_parts_and_stays_complete(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    offline = _OfflineSource(ARCHIVE)
    retry = _harness(tmp_path, offline)
    result = retry.service.run(symbol="BTCUSDT", data_type="aggTrades", start=START, end=END)

    assert result.run.status == "COMPLETE"
    assert result.run.files_verified == 7
    assert offline.opened == []  # nothing was re-downloaded
