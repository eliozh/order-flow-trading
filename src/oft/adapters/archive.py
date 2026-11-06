"""Archive adapter: HTTPS download, checksum verification, safe extraction.

Every archive is untrusted input. Downloads stream to a temporary file, the
published SHA256 is verified before the bytes are parsed, and extraction runs
in a brand-new empty directory with explicit traversal, symlink, encryption,
entry-count, size, and expansion-ratio limits. Archive content is only ever
read as data; nothing is executed.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import http.client
import os
import random
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from oft.domain.events import AggTrade
from oft.ports.archive import (
    ArchiveError,
    ArchiveFile,
    ArchiveStream,
    ChecksumFormatError,
    ChecksumMismatchError,
    DownloadError,
    UnsafeArchiveError,
)

BASE_URL = "https://data.binance.vision"
DAILY_PREFIX = "data/futures/um/daily"
USER_AGENT = "oft/0.1 (+local)"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
_CHUNK_SIZE = 64 * 1024
_HEX_LENGTH = 64
_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_AGG_TRADE_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)

OpenerCallable = Callable[..., Any]
SleepCallable = Callable[[float], None]


def daily_filename(symbol: str, data_type: str, day: date) -> str:
    return f"{symbol}-{data_type}-{day.isoformat()}.zip"


def daily_url(base_url: str, symbol: str, data_type: str, day: date) -> str:
    name = daily_filename(symbol, data_type, day)
    return f"{base_url.rstrip('/')}/{DAILY_PREFIX}/{data_type}/{symbol}/{name}"


def _dates(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ArchiveError("archive date range is inverted")
    days = (end - start).days
    return tuple(start + timedelta(days=offset) for offset in range(days + 1))


def _build_files(
    base_url: str, *, symbol: str, data_type: str, start: date, end: date
) -> tuple[ArchiveFile, ...]:
    return tuple(
        ArchiveFile(
            symbol=symbol,
            data_type=data_type,
            date=day,
            url=daily_url(base_url, symbol, data_type, day),
            checksum_url=daily_url(base_url, symbol, data_type, day) + ".CHECKSUM",
            filename=daily_filename(symbol, data_type, day),
        )
        for day in _dates(start, end)
    )


def parse_checksum_text(text: str) -> tuple[str, str]:
    """Parse ``<sha256>  <filename>`` (the ``data.binance.vision`` shape)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ChecksumFormatError("checksum file must contain exactly one digest line")
    parts = lines[0].split(None, 1)
    if len(parts) != 2:
        raise ChecksumFormatError("checksum file must contain a digest and a filename")
    digest, filename = parts[0].lower(), parts[1].strip()
    if not _HEX_PATTERN.fullmatch(digest):
        raise ChecksumFormatError("checksum digest must be 64 hexadecimal characters")
    if not filename:
        raise ChecksumFormatError("checksum filename is empty")
    return digest, filename


def sha256_file(path: Path, *, chunk_size: int = _CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_digest(expected: str) -> str:
    stripped = expected.strip()
    if " " in stripped or "\n" in stripped:
        return parse_checksum_text(stripped)[0]
    candidate = stripped.lower()
    if not _HEX_PATTERN.fullmatch(candidate):
        raise ChecksumFormatError("expected checksum must be 64 hexadecimal characters")
    return candidate


def verify_checksum(path: Path, expected: str) -> str:
    """Verify ``path`` against a checksum line or bare digest; return the digest."""
    expected_digest = _expected_digest(expected)
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected_digest):
        raise ChecksumMismatchError(
            f"archive CHECKSUM mismatch for {path.name}: expected {expected_digest}, got {actual}",
            expected=expected_digest,
            actual=actual,
        )
    return actual


# -- Safe extraction ---------------------------------------------------------


@dataclass(frozen=True)
class ExtractionLimits:
    """Bounds applied before and during streamed extraction."""

    max_entries: int = 1000
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_expansion_ratio: float = 200.0
    max_directories: int = 100
    max_path_depth: int = 8
    max_component_length: int = 255


@dataclass(frozen=True)
class ExtractionResult:
    """A completed extraction into a fresh directory owned by the caller."""

    directory: Path
    files: tuple[Path, ...]
    entries: int
    uncompressed_bytes: int


def _validate_entry(info: zipfile.ZipInfo, directory: Path, policy: ExtractionLimits) -> Path:
    if info.is_dir():
        raise UnsafeArchiveError(f"non-file archive entry rejected: {info.filename!r}")
    if info.flag_bits & 0x1:
        raise UnsafeArchiveError(f"encrypted archive entry rejected: {info.filename!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise UnsafeArchiveError(f"symlink archive entry rejected: {info.filename!r}")
    name = info.filename
    if not name or "\x00" in name:
        raise UnsafeArchiveError("archive entry name is empty or contains NUL")
    if "\\" in name:
        raise UnsafeArchiveError(f"backslash path separator rejected: {name!r}")
    if name.startswith("/") or _DRIVE_PATTERN.match(name):
        raise UnsafeArchiveError(f"absolute archive path rejected: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute():
        raise UnsafeArchiveError(f"absolute archive path rejected: {name!r}")
    if any(part in {"..", ".", ""} for part in relative.parts):
        raise UnsafeArchiveError(f"path traversal rejected: {name!r}")
    if len(relative.parts) > policy.max_path_depth:
        raise UnsafeArchiveError(f"archive path is too deep: {name!r}")
    if any(len(part) > policy.max_component_length for part in relative.parts):
        raise UnsafeArchiveError(f"archive path component is too long: {name!r}")
    destination = directory / relative
    if not destination.resolve().is_relative_to(directory.resolve()):
        raise UnsafeArchiveError(f"path escapes the extraction root: {name!r}")
    return destination


def _ensure_parents(
    destination: Path,
    directory: Path,
    created: set[Path],
    policy: ExtractionLimits,
) -> None:
    missing: list[Path] = []
    parent = destination.parent
    while parent != directory and parent != parent.parent:
        if parent.exists():
            if not parent.is_dir():
                raise UnsafeArchiveError(
                    f"archive entry conflicts with a file: {destination.name!r}"
                )
            break
        missing.append(parent)
        parent = parent.parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            if not path.is_dir():
                raise UnsafeArchiveError(
                    f"archive entry conflicts with a file: {destination.name!r}"
                ) from None
        created.add(path)
    if len(created) > policy.max_directories:
        raise UnsafeArchiveError(f"archive exceeds the {policy.max_directories} directory budget")


def _zip_eocd_entry_count(zip_path: Path) -> int | None:
    """Read the EOCD entry count before parsing the central directory."""
    try:
        size = zip_path.stat().st_size
        with zip_path.open("rb") as handle:
            handle.seek(max(0, size - 65_557))
            tail = handle.read()
    except OSError:
        return None
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 10 > len(tail):
        return None
    return int.from_bytes(tail[offset + 10 : offset + 12], "little")


def extract_zip_safely(
    zip_path: Path,
    *,
    workspace: Path,
    limits: ExtractionLimits | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ExtractionResult:
    """Stream a ZIP into a new empty directory under ``workspace``.

    The directory is removed on any failure. Entries are validated before
    writing; the archive is never extracted with ``extract``/``extractall``.
    """
    policy = limits or ExtractionLimits()
    if not workspace.is_dir():
        raise UnsafeArchiveError(f"extraction workspace is not a directory: {workspace}")
    declared_entries = _zip_eocd_entry_count(zip_path)
    if declared_entries is not None and declared_entries > policy.max_entries:
        raise UnsafeArchiveError(f"archive exceeds the {policy.max_entries} entry limit")
    directory = Path(tempfile.mkdtemp(prefix="oft-extract-", dir=workspace))
    extracted: list[Path] = []
    created_dirs: set[Path] = set()
    total_written = 0
    try:
        try:
            archive = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise UnsafeArchiveError(f"archive is not a valid ZIP file: {zip_path.name}") from exc
        with archive:
            entries = archive.infolist()
            if len(entries) > policy.max_entries:
                raise UnsafeArchiveError(f"archive exceeds the {policy.max_entries} entry limit")
            declared = sum(info.file_size for info in entries)
            if declared > policy.max_uncompressed_bytes:
                raise UnsafeArchiveError(
                    "archive exceeds the uncompressed size limit "
                    f"({declared} > {policy.max_uncompressed_bytes} bytes)"
                )
            archive_bytes = max(zip_path.stat().st_size, 1)
            if declared / archive_bytes > policy.max_expansion_ratio:
                raise UnsafeArchiveError(
                    "archive expansion ratio exceeds the limit "
                    f"({declared / archive_bytes:.1f} > {policy.max_expansion_ratio})"
                )
            for info in entries:
                destination = _validate_entry(info, directory, policy)
                _ensure_parents(destination, directory, created_dirs, policy)
                written = 0
                try:
                    with archive.open(info) as source, destination.open("xb") as target:
                        while chunk := source.read(_CHUNK_SIZE):
                            written += len(chunk)
                            total_written += len(chunk)
                            if total_written > policy.max_uncompressed_bytes:
                                raise UnsafeArchiveError(
                                    "archive exceeded the uncompressed size limit while streaming"
                                )
                            target.write(chunk)
                            if on_progress is not None:
                                on_progress(total_written)
                except FileExistsError as exc:
                    raise UnsafeArchiveError(
                        f"duplicate archive entry rejected: {info.filename!r}"
                    ) from exc
                except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError) as exc:
                    raise UnsafeArchiveError(
                        f"archive entry could not be read: {info.filename!r}"
                    ) from exc
                except (IsADirectoryError, NotADirectoryError) as exc:
                    raise UnsafeArchiveError(
                        f"archive entry conflicts with a directory: {info.filename!r}"
                    ) from exc
                extracted.append(destination)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return ExtractionResult(
        directory=directory,
        files=tuple(extracted),
        entries=len(extracted),
        uncompressed_bytes=total_written,
    )


# -- CSV parsing -------------------------------------------------------------


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise ArchiveError(f"invalid is_buyer_maker value {raw!r}; expected true or false")


def _parse_ms(raw: str) -> datetime:
    milliseconds = int(raw)
    return datetime.fromtimestamp(milliseconds // 1000, tz=UTC) + timedelta(
        milliseconds=milliseconds % 1000
    )


def extract_agg_trade_csv(
    path: Path, *, symbol: str, on_progress: Callable[[int], None] | None = None
) -> tuple[AggTrade, ...]:
    """Parse one extracted ``aggTrades`` CSV into normalized events."""
    trades: list[AggTrade] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = tuple(reader.fieldnames or ())
            missing = [column for column in _AGG_TRADE_COLUMNS if column not in fieldnames]
            if missing:
                raise ArchiveError(
                    f"aggTrades CSV {path.name} is missing columns: {', '.join(missing)}"
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    if None in row or any(row.get(column) is None for column in _AGG_TRADE_COLUMNS):
                        raise ValueError("row is shorter than the header")
                    trades.append(
                        AggTrade(
                            symbol=symbol,
                            agg_trade_id=int(row["agg_trade_id"]),
                            price=Decimal(row["price"]),
                            quantity=Decimal(row["quantity"]),
                            first_trade_id=int(row["first_trade_id"]),
                            last_trade_id=int(row["last_trade_id"]),
                            transact_time=_parse_ms(row["transact_time"]),
                            is_buyer_maker=_parse_bool(row["is_buyer_maker"]),
                        )
                    )
                except (
                    KeyError,
                    ValueError,
                    InvalidOperation,
                    TypeError,
                    AttributeError,
                    OverflowError,
                ) as exc:
                    raise ArchiveError(
                        f"invalid aggTrade row at line {line_number}: {exc}"
                    ) from exc
                if on_progress is not None and line_number % 10_000 == 0:
                    on_progress(line_number)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ArchiveError(f"cannot read extracted CSV {path}: {exc}") from exc
    if not trades:
        raise ArchiveError(f"aggTrades CSV {path.name} contains no rows")
    return tuple(trades)


# -- HTTPS source ------------------------------------------------------------


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


class _HttpStream:
    def __init__(self, response: Any, size: int | None) -> None:
        self._response = response
        self.size = size

    def read(self, size: int = -1) -> bytes:
        return bytes(self._response.read(size))

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> ArchiveStream:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _FileStream:
    size: int | None

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._handle = handle
        self.size = size

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> ArchiveStream:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class HttpsArchiveSource:
    """HTTPS archive source with timeouts, bounded reads, and backoff."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener: OpenerCallable | None = None,
        sleep: SleepCallable = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._base_url = base_url
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_bytes = max_bytes
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    def list_daily(
        self, *, symbol: str, data_type: str, start: date, end: date
    ) -> tuple[ArchiveFile, ...]:
        return _build_files(
            self._base_url, symbol=symbol, data_type=data_type, start=start, end=end
        )

    def _request(self, url: str, *, method: str = "GET") -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            try:
                return self._opener(request, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                transient = exc.code >= 500 or exc.code in {408, 425, 429}
                if not transient:
                    raise DownloadError(
                        f"archive request failed with HTTP {exc.code}: {url}"
                    ) from exc
                raw_retry_after = (
                    _header(dict(exc.headers.items()), "Retry-After") if exc.headers else None
                )
                if raw_retry_after and raw_retry_after.strip().isdigit():
                    retry_after = float(raw_retry_after.strip())
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                last_error = exc
            if attempt < self._max_attempts:
                delay = self._backoff_seconds * (2 ** (attempt - 1))
                delay += random.uniform(0.0, self._backoff_seconds * 0.25)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                self._sleep(delay)
        raise DownloadError(f"archive request failed after retries: {url}") from last_error

    def read_checksum(self, file: ArchiveFile) -> str:
        response = self._request(file.checksum_url)
        try:
            try:
                body = response.read(MAX_CHECKSUM_BYTES + 1)
            except (http.client.HTTPException, OSError) as exc:
                raise DownloadError(f"checksum download failed: {file.checksum_url}") from exc
        finally:
            response.close()
        if len(body) > MAX_CHECKSUM_BYTES:
            raise DownloadError(f"checksum file is unexpectedly large: {file.checksum_url}")
        try:
            return str(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise DownloadError(f"checksum file is not UTF-8: {file.checksum_url}") from exc

    def expected_size(self, file: ArchiveFile) -> int | None:
        try:
            response = self._request(file.url, method="HEAD")
        except DownloadError:
            return None
        try:
            raw = _header(response.headers, "Content-Length")
        finally:
            response.close()
        if raw is None or not raw.strip().isdigit():
            return None
        return int(raw)

    def open(self, file: ArchiveFile) -> _HttpStream:
        response = self._request(file.url)
        raw = _header(response.headers, "Content-Length")
        size = int(raw) if raw is not None and raw.strip().isdigit() else None
        if size is not None and size > self._max_bytes:
            response.close()
            raise DownloadError(
                f"archive exceeds the {self._max_bytes} byte download cap: {file.filename}"
            )
        return _HttpStream(response, size)


class FixtureArchiveSource:
    """Network-free source backed by committed fixture archives."""

    def __init__(self, fixtures_dir: Path | str, *, base_url: str = BASE_URL) -> None:
        self._fixtures_dir = Path(fixtures_dir)
        self._base_url = base_url

    @property
    def fixtures_dir(self) -> Path:
        return self._fixtures_dir

    def list_daily(
        self, *, symbol: str, data_type: str, start: date, end: date
    ) -> tuple[ArchiveFile, ...]:
        return _build_files(
            self._base_url, symbol=symbol, data_type=data_type, start=start, end=end
        )

    def _local(self, file: ArchiveFile) -> Path:
        root = self._fixtures_dir.resolve()
        canonical = (root / file.filename).resolve()
        # Committed fixtures use the lower-case ``symbol_type_date`` convention
        # from the slice brief; map the canonical archive name onto it.
        snake_type = re.sub(r"(?<!^)(?=[A-Z])", "-", file.data_type).lower()
        fallback = (
            root / f"{file.symbol.lower()}_{snake_type}_{file.date.isoformat()}.zip"
        ).resolve()
        for candidate in (canonical, fallback):
            if not candidate.is_relative_to(root):
                raise UnsafeArchiveError(
                    f"fixture path escapes the fixtures directory: {candidate.name}"
                )
            if candidate.exists():
                return candidate
        return fallback

    def read_checksum(self, file: ArchiveFile) -> str:
        local = self._local(file)
        path = local.with_name(local.name + ".CHECKSUM")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DownloadError(f"fixture checksum is missing: {path}") from exc

    def expected_size(self, file: ArchiveFile) -> int | None:
        try:
            return self._local(file).stat().st_size
        except OSError:
            return None

    def open(self, file: ArchiveFile) -> _FileStream:
        path = self._local(file)
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise DownloadError(f"fixture archive is missing: {path}") from exc
        return _FileStream(handle, path.stat().st_size)


# Kept for adapters that need to copy a stream to disk without buffering.
class UnconfiguredArchiveSource:
    """Explicit failure when a test environment has no fixture archive."""

    def __init__(self, message: str) -> None:
        self._message = message

    def list_daily(
        self, *, symbol: str, data_type: str, start: date, end: date
    ) -> tuple[ArchiveFile, ...]:
        raise DownloadError(self._message)

    def read_checksum(self, file: ArchiveFile) -> str:
        raise DownloadError(self._message)

    def expected_size(self, file: ArchiveFile) -> int | None:
        raise DownloadError(self._message)

    def open(self, file: ArchiveFile) -> ArchiveStream:
        raise DownloadError(self._message)


def stream_to_file(
    stream: Any,
    destination: Path,
    *,
    chunk_size: int = _CHUNK_SIZE,
    max_bytes: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Stream ``stream`` into a new file (0600, no-follow) and return bytes written."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise DownloadError(f"temporary download already exists: {destination.name}") from exc
    written = 0
    with os.fdopen(descriptor, "wb") as handle:
        while chunk := stream.read(chunk_size):
            written += len(chunk)
            if max_bytes is not None and written > max_bytes:
                raise DownloadError(f"download exceeded the {max_bytes} byte cap while streaming")
            handle.write(chunk)
            if on_progress is not None:
                on_progress(written)
        handle.flush()
        os.fsync(handle.fileno())
    return written


__all__ = [
    "ExtractionLimits",
    "ExtractionResult",
    "FixtureArchiveSource",
    "HttpsArchiveSource",
    "daily_filename",
    "daily_url",
    "extract_agg_trade_csv",
    "extract_zip_safely",
    "parse_checksum_text",
    "sha256_file",
    "stream_to_file",
    "verify_checksum",
]
