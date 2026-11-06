"""Stale RUNNING backfill runs are recovered instead of blocking forever."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.store import BackfillRunRecord


def _record(run_id: str, started_at: str) -> BackfillRunRecord:
    return BackfillRunRecord(
        run_id=run_id,
        symbol="BTCUSDT",
        data_type="aggTrades",
        start_date="2026-10-01",
        end_date="2026-10-07",
        status="RUNNING",
        started_at=started_at,
        finished_at=None,
        estimated_bytes=0,
        processed_bytes=0,
        files_total=7,
        files_verified=0,
        files_failed=0,
        error_code=None,
        message=None,
    )


def test_stale_running_run_is_recovered_and_a_fresh_run_still_blocks(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()

    assert store.start_backfill_run(_record("BF-STALE", "2020-01-01T00:00:00.000Z")) is True

    fresh_started = utc_timestamp(datetime.now(UTC))
    assert store.start_backfill_run(_record("BF-NEW", fresh_started)) is True
    stale = store.get_backfill_run("BF-STALE")
    assert stale is not None and stale.status == "STALE"
    assert store.get_backfill_run("BF-NEW") is not None

    # The new run is fresh, so a concurrent start is still rejected.
    assert store.start_backfill_run(_record("BF-THIRD", fresh_started)) is False


def test_heartbeat_prevents_a_long_run_from_being_stolen(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()

    assert store.start_backfill_run(_record("BF-LONG", "2020-01-01T00:00:00.000Z")) is True
    # The (legitimately long) run keeps heartbeating; it must not be evicted.
    store.touch_backfill_run("BF-LONG", updated_at=utc_timestamp(datetime.now(UTC)))

    fresh_started = utc_timestamp(datetime.now(UTC))
    assert store.start_backfill_run(_record("BF-NEW", fresh_started)) is False
    long_run = store.get_backfill_run("BF-LONG")
    assert long_run is not None and long_run.status == "RUNNING"


def test_heavyweight_run_lease_requires_the_owner(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    started = utc_timestamp(datetime.now(UTC))

    assert store.start_backfill_run(_record("BF-OWNED", started), owner="owner-a") is True
    assert (
        store.touch_backfill_run(
            "BF-OWNED", updated_at=utc_timestamp(datetime.now(UTC)), owner="owner-b"
        )
        is False
    )
    assert (
        store.touch_backfill_run(
            "BF-OWNED", updated_at=utc_timestamp(datetime.now(UTC)), owner="owner-a"
        )
        is True
    )
    assert store.save_backfill_run(_record("BF-OWNED", started), owner="owner-b") is False


def test_commit_is_owner_scoped(tmp_path: Path) -> None:
    from oft.ports.store import BackfillManifestRow, LeaseLostError

    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    started = utc_timestamp(datetime.now(UTC))
    assert store.start_backfill_run(_record("BF-OWNED", started), owner="owner-a") is True
    row = BackfillManifestRow(
        file_key="aggTrades/BTCUSDT/2026-10-01",
        run_id="BF-OWNED",
        data_type="aggTrades",
        symbol="BTCUSDT",
        date="2026-10-01",
        source_url="u",
        checksum="c",
        checksum_verified=True,
        status="COMPLETE",
        bytes=1,
        rows=1,
        downloaded_at=started,
        verified_at=started,
        error_code=None,
    )

    with pytest.raises(LeaseLostError):
        store.commit_backfill_file(row=row, entries=(), resolved_at=started, owner="owner-b")
    assert store.get_backfill_manifest(row.file_key) is None

    store.commit_backfill_file(row=row, entries=(), resolved_at=started, owner="owner-a")
    assert store.get_backfill_manifest(row.file_key) is not None
