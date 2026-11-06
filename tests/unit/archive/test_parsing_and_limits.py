"""CSV parsing errors and ZIP entry limits fail closed."""

from __future__ import annotations

import http.client
import zipfile
from datetime import date
from pathlib import Path

import pytest

from oft.adapters.archive import (
    HttpsArchiveSource,
    extract_agg_trade_csv,
    extract_zip_safely,
)
from oft.ports.archive import ArchiveError, DownloadError, UnsafeArchiveError

_HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"


def test_csv_short_row_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "short.csv"
    path.write_text(_HEADER + "1,100.0\n", encoding="utf-8")

    with pytest.raises(ArchiveError):
        extract_agg_trade_csv(path, symbol="BTCUSDT")


def test_csv_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_bytes(b"\xff\xfe\x00not utf-8")

    with pytest.raises(ArchiveError):
        extract_agg_trade_csv(path, symbol="BTCUSDT")


def test_zip_path_depth_is_bounded(tmp_path: Path) -> None:
    archive = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a/b/c/d/e/f/g/h/i.csv", "x")

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(archive, workspace=tmp_path)


def test_zip_file_directory_conflict_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "conflict.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a", "not a directory")
        handle.writestr("a/b.csv", "x")

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(archive, workspace=tmp_path)


class _RaisingResponse:
    headers: dict[str, str] = {}

    def read(self, _size: int = -1) -> bytes:
        raise http.client.IncompleteRead(b"partial")

    def close(self) -> None:
        return None


def test_checksum_read_incomplete_read_maps_to_download_error() -> None:
    source = HttpsArchiveSource(
        opener=lambda request, timeout: _RaisingResponse(),
        max_attempts=1,
    )
    file = source.list_daily(
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
    )[0]

    with pytest.raises(DownloadError):
        source.read_checksum(file)


def test_fixture_source_rejects_a_path_outside_the_fixtures_dir(tmp_path: Path) -> None:
    from oft.adapters.archive import FixtureArchiveSource
    from oft.ports.archive import ArchiveFile

    source = FixtureArchiveSource(tmp_path)
    file = ArchiveFile(
        symbol="BTCUSDT",
        data_type="aggTrades",
        date=date(2026, 10, 1),
        url="",
        checksum_url="",
        filename="../../evil.zip",
    )

    with pytest.raises(UnsafeArchiveError):
        source._local(file)


@pytest.mark.parametrize(
    "failure", [NotImplementedError("compression 99"), EOFError("truncated deflate")]
)
def test_zip_entry_read_failures_are_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    archive = tmp_path / "entry-error.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a.csv", "x")

    def boom(self, info, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise failure

    monkeypatch.setattr(zipfile.ZipFile, "open", boom)

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(archive, workspace=tmp_path)
