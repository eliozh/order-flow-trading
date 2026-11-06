"""Checksum parsing/verification and HTTPS archive download behavior."""

from __future__ import annotations

import os
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.archive import (
    ExtractionLimits,
    FixtureArchiveSource,
    HttpsArchiveSource,
    extract_agg_trade_csv,
    extract_zip_safely,
    parse_checksum_text,
    sha256_file,
    verify_checksum,
)
from oft.ports.archive import ChecksumMismatchError, DownloadError

ARCHIVE = Path(__file__).resolve().parents[2] / "fixtures" / "archive"
VALID_ZIP = ARCHIVE / "btcusdt_agg-trades_2026-10-01.zip"
VALID_CHECKSUM = ARCHIVE / "btcusdt_agg-trades_2026-10-01.zip.CHECKSUM"
CORRUPT_ZIP = ARCHIVE / "corrupt" / "btcusdt_agg-trades_2026-10-08.zip"
CORRUPT_CHECKSUM = CORRUPT_ZIP.with_name(CORRUPT_ZIP.name + ".CHECKSUM")


def test_parse_checksum_text_accepts_the_binance_shape() -> None:
    expected = VALID_CHECKSUM.read_text(encoding="utf-8")

    digest, filename = parse_checksum_text(expected)

    assert len(digest) == 64
    assert filename == VALID_ZIP.name
    assert digest == sha256_file(VALID_ZIP)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not-a-digest  file.zip\n",
        "abc123  file.zip\n",
        "0" * 64,
        f"{'0' * 64}  one.zip\n{'1' * 64}  two.zip\n",
        f"{'z' * 64}  file.zip\n",
    ],
)
def test_parse_checksum_text_rejects_malformed_input(text: str) -> None:
    with pytest.raises(Exception) as excinfo:
        parse_checksum_text(text)

    assert getattr(excinfo.value, "code", None) in {
        "DATA_UNAVAILABLE",
        "BACKFILL_CHECKSUM_MISMATCH",
    }


def test_verify_checksum_accepts_the_matching_file() -> None:
    verify_checksum(VALID_ZIP, VALID_CHECKSUM.read_text(encoding="utf-8"))


def test_verify_checksum_rejects_a_mismatch_with_the_catalog_code() -> None:
    with pytest.raises(ChecksumMismatchError) as excinfo:
        verify_checksum(VALID_ZIP, CORRUPT_CHECKSUM.read_text(encoding="utf-8"))

    error = excinfo.value
    assert error.code == "BACKFILL_CHECKSUM_MISMATCH"
    assert error.expected == "0" * 64
    assert error.actual == sha256_file(VALID_ZIP)
    assert "CHECKSUM" in str(error)


def test_corrupt_fixture_zip_does_not_match_its_checksum() -> None:
    with pytest.raises(ChecksumMismatchError):
        verify_checksum(CORRUPT_ZIP, CORRUPT_CHECKSUM.read_text(encoding="utf-8"))


# -- HTTPS source (no network: an injected opener serves all requests) --------


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _FakeOpener:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        method = request.get_method()
        url = request.full_url
        self.calls.append((method, url))
        response = self._responses[(method, url)]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response()
        return response


def _source(
    responses: dict[tuple[str, str], Any], *, sleeps: list[float] | None = None
) -> HttpsArchiveSource:
    recorded: list[float] = [] if sleeps is None else sleeps
    return HttpsArchiveSource(
        opener=_FakeOpener(responses),
        sleep=recorded.append,
        backoff_seconds=0.01,
        max_attempts=3,
    )


def test_list_daily_builds_archive_urls_and_names() -> None:
    source = _source({})
    files = source.list_daily(
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=date(2026, 10, 1),
        end=date(2026, 10, 3),
    )

    assert [item.date.isoformat() for item in files] == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]
    assert files[0].url.endswith(
        "/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-10-01.zip"
    )
    assert files[0].checksum_url == files[0].url + ".CHECKSUM"
    assert files[0].filename == "BTCUSDT-aggTrades-2026-10-01.zip"


def test_read_checksum_and_expected_size_use_the_remote_metadata() -> None:
    checksum_body = VALID_CHECKSUM.read_bytes()
    file = _source({}).list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]
    opener = _FakeOpener(
        {
            ("GET", file.checksum_url): _FakeResponse(checksum_body),
            ("HEAD", file.url): _FakeResponse(b"", headers={"Content-Length": "284"}),
        }
    )
    source = HttpsArchiveSource(opener=opener, sleep=lambda _: None)

    assert source.read_checksum(file) == checksum_body.decode("utf-8")
    assert source.expected_size(file) == 284


def test_open_streams_the_archive_and_enforces_the_size_cap() -> None:
    file = _source({}).list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]
    body = VALID_ZIP.read_bytes()
    source = _source(
        {("GET", file.url): _FakeResponse(body, headers={"Content-Length": str(len(body))})}
    )

    with source.open(file) as stream:
        assert stream.size == len(body)
        assert stream.read(8) == body[:8]


def test_oversized_download_is_rejected_before_reading_the_body() -> None:
    file = _source({}).list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]
    source = HttpsArchiveSource(
        opener=_FakeOpener(
            {("GET", file.url): _FakeResponse(b"x", headers={"Content-Length": "999999999"})}
        ),
        sleep=lambda _: None,
        max_bytes=1024,
    )

    with pytest.raises(DownloadError):
        source.open(file)


def test_transient_http_errors_are_retried_with_backoff() -> None:
    file = _source({}).list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]
    sleeps: list[float] = []
    responses: dict[tuple[str, str], Any] = {("GET", file.url): _FakeResponse(b"payload")}
    opener = _FakeOpener(responses)
    attempts = {"count": 0}

    def flaky(request: Any, timeout: float | None = None) -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "busy",
                {},
                None,  # type: ignore[arg-type]
            )
        return opener(request, timeout)

    source = HttpsArchiveSource(opener=flaky, sleep=sleeps.append, backoff_seconds=0.25)

    with source.open(file) as stream:
        assert stream.read() == b"payload"
    assert sleeps == [0.25]


def test_exhausted_retries_raise_download_error() -> None:
    file = _source({}).list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]
    sleeps: list[float] = []

    def always_timeout(request: Any, timeout: float | None = None) -> Any:
        raise TimeoutError("timed out")

    source = HttpsArchiveSource(
        opener=always_timeout, sleep=sleeps.append, backoff_seconds=0.5, max_attempts=3
    )

    with pytest.raises(DownloadError):
        source.open(file)
    assert sleeps == [0.5, 1.0]


def test_fixture_source_round_trips_a_committed_archive(tmp_path: Path) -> None:
    source = FixtureArchiveSource(ARCHIVE)
    file = source.list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]

    assert source.expected_size(file) == VALID_ZIP.stat().st_size
    assert source.read_checksum(file) == VALID_CHECKSUM.read_text(encoding="utf-8")
    destination = tmp_path / "copy.zip"
    with source.open(file) as stream, destination.open("wb") as handle:
        while chunk := stream.read(4096):
            handle.write(chunk)
    assert sha256_file(destination) == sha256_file(VALID_ZIP)


def test_fixture_source_missing_file_raises_download_error(tmp_path: Path) -> None:
    source = FixtureArchiveSource(tmp_path)
    file = source.list_daily(
        symbol="BTCUSDT", data_type="aggTrades", start=date(2026, 10, 1), end=date(2026, 10, 1)
    )[0]

    with pytest.raises(DownloadError):
        source.open(file)


# -- CSV conversion ----------------------------------------------------------


def test_extract_and_parse_agg_trades_from_a_verified_zip(tmp_path: Path) -> None:
    result = extract_zip_safely(VALID_ZIP, workspace=tmp_path, limits=ExtractionLimits())
    csv_files = tuple(path for path in result.files if path.suffix == ".csv")

    trades = extract_agg_trade_csv(csv_files[0], symbol="BTCUSDT")

    assert result.entries == 1
    assert len(trades) == 3
    assert trades[0].symbol == "BTCUSDT"
    assert trades[0].agg_trade_id == 1
    assert str(trades[0].price) == "70000.00"


@pytest.mark.live_smoke
@pytest.mark.skipif(
    os.environ.get("OFT_LIVE_SMOKE") != "1",
    reason="live archive download is opt-in (set OFT_LIVE_SMOKE=1)",
)
def test_live_download_one_day_archive() -> None:
    source = HttpsArchiveSource()
    file = source.list_daily(
        symbol="BTCUSDT",
        data_type="aggTrades",
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
    )[0]
    checksum = source.read_checksum(file)
    assert len(parse_checksum_text(checksum)[0]) == 64
