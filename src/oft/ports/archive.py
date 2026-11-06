"""Archive source port: URLs, checksums, streams, and size metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class ArchiveError(Exception):
    """Base class for typed archive failures; ``code`` matches the catalog."""

    code = "DATA_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ChecksumFormatError(ArchiveError):
    """The ``.CHECKSUM`` payload is missing or malformed."""


class ChecksumMismatchError(ArchiveError):
    """A downloaded archive does not match its published SHA256."""

    code = "BACKFILL_CHECKSUM_MISMATCH"

    def __init__(self, message: str, *, expected: str, actual: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class DownloadError(ArchiveError):
    """An archive could not be fetched or was truncated."""


class UnsafeArchiveError(ArchiveError):
    """An archive entry violates the extraction safety policy."""

    code = "VALIDATION_FAILED"


@dataclass(frozen=True)
class ArchiveFile:
    """One daily archive object and its sibling checksum object."""

    symbol: str
    data_type: str
    date: date
    url: str
    checksum_url: str
    filename: str


class ArchiveStream(Protocol):
    """A readable, closeable byte stream with optional total size."""

    size: int | None

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> ArchiveStream: ...

    def __exit__(self, *args: object) -> None: ...


class ArchiveSourcePort(Protocol):
    """Source of ``data.binance.vision`` daily objects.

    Adapters are responsible for transport (timeouts, retries, streaming) and
    for returning the checksum text; verification lives in the adapter module
    so every caller shares exactly one implementation.
    """

    def list_daily(
        self, *, symbol: str, data_type: str, start: date, end: date
    ) -> tuple[ArchiveFile, ...]: ...

    def open(self, file: ArchiveFile) -> ArchiveStream: ...

    def read_checksum(self, file: ArchiveFile) -> str: ...

    def expected_size(self, file: ArchiveFile) -> int | None: ...
