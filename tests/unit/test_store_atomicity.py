"""Scan + manifest persistence is a single transaction (unit of work)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.config import Settings
from oft.ports.store import ManifestEntry, ScanRecord


def _record(scan_id: str) -> ScanRecord:
    return ScanRecord(
        scan_id=scan_id,
        symbol="BTCUSDT",
        period="4h",
        status="DONE",
        trigger_source="MANUAL",
        metrics_only=True,
        fixture_path="fixture.csv",
        started_at="2026-11-06T00:00:00.000Z",
        finished_at="2026-11-06T00:00:01.000Z",
        window_start="2025-11-05T00:00:00.000Z",
        window_end="2025-11-05T04:00:00.000Z",
        trade_count=1,
        buy_volume="1",
        sell_volume="0",
        delta="1",
        cvd="1",
        data_quality="COMPLETE",
        manifest_path="/tmp/part.parquet",
        parquet_rows=1,
        created_at="2026-11-06T00:00:01.000Z",
    )


def _entry(path: str) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        data_type="agg_trades",
        symbol="BTCUSDT",
        date="2025-11-05",
        rows=1,
        bytes=10,
        checksum="abc",
        source="test",
        created_at="2026-11-06T00:00:01.000Z",
    )


def test_scan_and_manifest_roll_back_together(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    entry = _entry("/tmp/part.parquet")
    store.save_scan_with_manifest(_record("SCAN-1"), entry)

    with pytest.raises(sqlite3.IntegrityError):
        # Duplicate manifest path fails the second insert; the scan row from the
        # same unit of work must not be committed either.
        store.save_scan_with_manifest(_record("SCAN-2"), entry)

    assert store.get_scan("SCAN-2") is None
    assert len(store.list_manifest()) == 1
