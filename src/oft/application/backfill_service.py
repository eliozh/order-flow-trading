"""Resumable one-file-per-day archive backfill.

Flow per file: download to a temp file -> verify the published SHA256 ->
stream-extract into a fresh empty directory -> parse CSV -> dedupe/publish
Parquet -> upsert the manifest row. A checksum mismatch fails closed: no
Parquet part is published and the manifest row is never ``COMPLETE``.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol

import structlog

from oft.adapters.archive import (
    extract_agg_trade_csv,
    extract_zip_safely,
    parse_checksum_text,
    stream_to_file,
    verify_checksum,
)
from oft.application.gap_detection import detect_backfill_gaps
from oft.config import Settings
from oft.domain.events import AggTrade
from oft.domain.time import utc_timestamp
from oft.ports.archive import (
    ArchiveError,
    ArchiveFile,
    ArchiveSourcePort,
    ChecksumMismatchError,
    UnsafeArchiveError,
)
from oft.ports.clock import ClockPort
from oft.ports.store import (
    BackfillManifestRow,
    BackfillRunRecord,
    ManifestEntry,
    StorePort,
    StoreUnavailableError,
)

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
DATA_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
MAX_BACKFILL_DAYS = 31
DEFAULT_ESTIMATED_FILE_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024


class BackfillError(Exception):
    """Base class for typed backfill failures; ``code`` matches the catalog."""

    code = "DATA_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BackfillValidationError(BackfillError):
    code = "VALIDATION_FAILED"


class BackfillAlreadyRunning(BackfillError):
    code = "BACKFILL_ALREADY_RUNNING"


class ParquetWriterPort(Protocol):
    def write_agg_trades_batch(
        self,
        trades: tuple[AggTrade, ...] | list[AggTrade],
        *,
        symbol: str,
        source: str,
    ) -> tuple[ManifestEntry, ...]: ...


@dataclass(frozen=True)
class SizeEstimate:
    """Sizes computed from remote metadata before any download happens."""

    symbol: str
    data_type: str
    start: str
    end: str
    days: int
    files: int
    estimated_bytes: int
    estimated_mb: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BackfillResult:
    """The persisted run plus its per-file manifest rows."""

    estimate: SizeEstimate
    run: BackfillRunRecord
    files: tuple[BackfillManifestRow, ...]


def file_key(data_type: str, symbol: str, day: date | str) -> str:
    value = day.isoformat() if isinstance(day, date) else day
    return f"{data_type}/{symbol}/{value}"


def _validate(symbol: str, data_type: str, start: date, end: date) -> None:
    if SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise BackfillValidationError("symbol must match ^[A-Z0-9]{2,24}$")
    if DATA_TYPE_PATTERN.fullmatch(data_type) is None:
        raise BackfillValidationError("data_type must be an archive directory name")
    if end < start:
        raise BackfillValidationError("start date must not be after end date")
    if (end - start).days + 1 > MAX_BACKFILL_DAYS:
        raise BackfillValidationError(f"backfill range exceeds the {MAX_BACKFILL_DAYS}-day limit")


class BackfillService:
    """Orchestrates archive downloads, verification, conversion, and manifests."""

    def __init__(
        self,
        *,
        settings: Settings,
        archive: ArchiveSourcePort,
        parquet: ParquetWriterPort,
        store: StorePort,
        clock: ClockPort,
    ) -> None:
        self._settings = settings
        self._archive = archive
        self._parquet = parquet
        self._store = store
        self._clock = clock

    # -- Estimation ------------------------------------------------------

    def estimate(self, *, symbol: str, data_type: str, start: date, end: date) -> SizeEstimate:
        _validate(symbol, data_type, start, end)
        files = self._archive.list_daily(symbol=symbol, data_type=data_type, start=start, end=end)
        total = 0
        for file in files:
            size = self._archive.expected_size(file)
            total += DEFAULT_ESTIMATED_FILE_BYTES if size is None else size
        return SizeEstimate(
            symbol=symbol,
            data_type=data_type,
            start=start.isoformat(),
            end=end.isoformat(),
            days=len(files),
            files=len(files),
            estimated_bytes=total,
            estimated_mb=round(total / 1_000_000, 3),
        )

    # -- Run -------------------------------------------------------------

    def run(
        self,
        *,
        symbol: str,
        data_type: str,
        start: date,
        end: date,
        run_id: str | None = None,
    ) -> BackfillResult:
        _validate(symbol, data_type, start, end)
        estimate = self.estimate(symbol=symbol, data_type=data_type, start=start, end=end)
        files = self._archive.list_daily(symbol=symbol, data_type=data_type, start=start, end=end)
        started_at = utc_timestamp(self._clock.now())
        record = BackfillRunRecord(
            run_id=run_id or f"BF-{self._clock.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            symbol=symbol,
            data_type=data_type,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            status="RUNNING",
            started_at=started_at,
            finished_at=None,
            estimated_bytes=estimate.estimated_bytes,
            processed_bytes=0,
            files_total=len(files),
            files_verified=0,
            files_failed=0,
            error_code=None,
            message=None,
        )
        if not self._store.start_backfill_run(record):
            raise BackfillAlreadyRunning("A backfill is already running.")

        processed = 0
        verified = 0
        failed = 0
        try:
            for file in files:
                key = file_key(file.data_type, file.symbol, file.date)
                existing = self._store.get_backfill_manifest(key)
                if (
                    existing is not None
                    and existing.status == "COMPLETE"
                    and existing.checksum_verified
                ):
                    verified += 1
                    processed += existing.bytes
                    self._heartbeat(record)
                    continue
                try:
                    row = self._process_file(record, file)
                except ChecksumMismatchError as exc:
                    failed += 1
                    self._record_failure(record, file, exc.code)
                    finished = replace(
                        record,
                        status="FAILED",
                        finished_at=utc_timestamp(self._clock.now()),
                        processed_bytes=processed,
                        files_verified=verified,
                        files_failed=failed,
                        error_code=exc.code,
                        message=str(exc),
                    )
                    self._store.save_backfill_run(finished)
                    structlog.get_logger(__name__).error(
                        "backfill_checksum_mismatch",
                        symbol=file.symbol,
                        date=file.date.isoformat(),
                        url=file.url,
                        expected=exc.expected,
                        actual=exc.actual,
                    )
                    raise
                except UnsafeArchiveError as exc:
                    failed += 1
                    self._record_failure(record, file, exc.code)
                    finished = replace(
                        record,
                        status="BLOCKED",
                        finished_at=utc_timestamp(self._clock.now()),
                        processed_bytes=processed,
                        files_verified=verified,
                        files_failed=failed,
                        error_code=exc.code,
                        message=str(exc),
                    )
                    self._store.save_backfill_run(finished)
                    structlog.get_logger(__name__).error(
                        "backfill_archive_rejected",
                        symbol=file.symbol,
                        date=file.date.isoformat(),
                        reason=str(exc),
                    )
                    raise
                except ArchiveError as exc:
                    failed += 1
                    self._record_failure(record, file, exc.code)
                    structlog.get_logger(__name__).warning(
                        "backfill_file_failed",
                        symbol=file.symbol,
                        date=file.date.isoformat(),
                        reason=str(exc),
                    )
                    self._heartbeat(record)
                    continue
                verified += 1
                processed += row.bytes
                self._heartbeat(record)
        except (ChecksumMismatchError, UnsafeArchiveError):
            raise
        except StoreUnavailableError as exc:
            finished = replace(
                record,
                status="FAILED",
                finished_at=utc_timestamp(self._clock.now()),
                processed_bytes=processed,
                files_verified=verified,
                files_failed=failed,
                error_code=exc.code,
                message=exc.message,
            )
            self._store.save_backfill_run(finished)
            raise
        except Exception as exc:  # pragma: no cover - defensive run bookkeeping
            finished = replace(
                record,
                status="FAILED",
                finished_at=utc_timestamp(self._clock.now()),
                processed_bytes=processed,
                files_verified=verified,
                files_failed=failed,
                error_code="INTERNAL_ERROR",
                message=str(exc),
            )
            self._store.save_backfill_run(finished)
            raise

        status = "COMPLETE" if failed == 0 else "PARTIAL"
        final = replace(
            record,
            status=status,
            finished_at=utc_timestamp(self._clock.now()),
            processed_bytes=processed,
            files_verified=verified,
            files_failed=failed,
        )
        self._store.save_backfill_run(final)
        # AC4 reachability: every completed/partial run refreshes the persisted
        # gap set for its window so /api/v1/data/quality reflects reality.
        detect_backfill_gaps(
            self._settings,
            self._store,
            symbol=symbol,
            data_type=data_type,
            start=start,
            end=end,
            clock=self._clock,
            run_id=record.run_id,
        )
        rows = tuple(
            row
            for row in self._store.list_backfill_manifest(symbol=symbol, data_type=data_type)
            if start.isoformat() <= row.date <= end.isoformat()
        )
        structlog.get_logger(__name__).info(
            "backfill_finished",
            run_id=record.run_id,
            symbol=symbol,
            status=status,
            files_verified=verified,
            files_failed=failed,
        )
        return BackfillResult(estimate=estimate, run=final, files=rows)

    # -- Per-file work ---------------------------------------------------

    def _work_dir(self) -> Path:
        path = self._settings.data_dir / "run" / "extract"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _raw_dir(self, file: ArchiveFile) -> Path:
        return self._settings.data_dir / "raw" / "um" / "daily" / file.data_type / file.symbol

    def _download(self, file: ArchiveFile) -> tuple[Path, int, str]:
        expected = parse_checksum_text(self._archive.read_checksum(file))[0]
        raw_dir = self._raw_dir(file)
        raw_dir.mkdir(parents=True, exist_ok=True)
        final_path = raw_dir / file.filename
        if final_path.is_file():
            try:
                verify_checksum(final_path, expected)
            except ChecksumMismatchError:
                final_path.unlink(missing_ok=True)
            else:
                return final_path, final_path.stat().st_size, expected

        temp_path = raw_dir / f".{file.filename}.{uuid.uuid4().hex}.part"
        try:
            try:
                with self._archive.open(file) as stream:
                    size = stream_to_file(stream, temp_path, max_bytes=MAX_DOWNLOAD_BYTES)
            except (OSError, http.client.HTTPException) as exc:
                # A mid-stream truncation/timeout is a retryable download
                # failure, not an unexpected internal error.
                raise ArchiveError(f"archive download failed: {exc}") from exc
            verify_checksum(temp_path, expected)
            os.replace(temp_path, final_path)
            if final_path.stat().st_size != size:
                raise ArchiveError(f"downloaded size changed for {file.filename}")
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return final_path, final_path.stat().st_size, expected

    def _process_file(self, run: BackfillRunRecord, file: ArchiveFile) -> BackfillManifestRow:
        zip_path, size, expected = self._download(file)
        extraction = extract_zip_safely(zip_path, workspace=self._work_dir())
        entries: tuple[ManifestEntry, ...] = ()
        try:
            csv_files = tuple(path for path in extraction.files if path.suffix.lower() == ".csv")
            if not csv_files:
                raise ArchiveError(f"archive contains no CSV file: {file.filename}")
            trades: list[AggTrade] = []
            for csv_path in csv_files:
                trades.extend(extract_agg_trade_csv(csv_path, symbol=file.symbol))
            entries = self._parquet.write_agg_trades_batch(
                trades, symbol=file.symbol, source=f"archive:{file.url}"
            )
            now = utc_timestamp(self._clock.now())
            row = BackfillManifestRow(
                file_key=file_key(file.data_type, file.symbol, file.date),
                run_id=run.run_id,
                data_type=file.data_type,
                symbol=file.symbol,
                date=file.date.isoformat(),
                source_url=file.url,
                checksum=expected,
                checksum_verified=True,
                status="COMPLETE",
                bytes=size,
                rows=sum(entry.rows for entry in entries),
                downloaded_at=now,
                verified_at=now,
                error_code=None,
            )
            self._store.upsert_backfill_manifest(row)
            # A verified, published day resolves any earlier gap for that day.
            self._store.resolve_data_gaps_for_date(
                symbol=file.symbol,
                data_type=file.data_type,
                day=file.date.isoformat(),
                resolved_at=now,
            )
            return row
        except Exception:
            # Never leave a published part behind for a file that failed.
            for entry in entries:
                Path(entry.path).unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(extraction.directory, ignore_errors=True)

    def _heartbeat(self, run: BackfillRunRecord) -> None:
        self._store.touch_backfill_run(run.run_id, updated_at=utc_timestamp(self._clock.now()))

    def _record_failure(self, run: BackfillRunRecord, file: ArchiveFile, code: str) -> None:
        size = self._archive.expected_size(file) or 0
        now = utc_timestamp(self._clock.now())
        self._store.upsert_backfill_manifest(
            BackfillManifestRow(
                file_key=file_key(file.data_type, file.symbol, file.date),
                run_id=run.run_id,
                data_type=file.data_type,
                symbol=file.symbol,
                date=file.date.isoformat(),
                source_url=file.url,
                checksum="",
                checksum_verified=False,
                status="FAILED",
                bytes=size,
                rows=0,
                downloaded_at=now,
                verified_at=None,
                error_code=code,
            )
        )
