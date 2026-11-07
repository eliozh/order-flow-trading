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
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
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
from oft.adapters.parquet_store import recover_orphan_parts
from oft.application.gap_detection import SUPPORTED_DATA_TYPES, detect_backfill_gaps
from oft.application.rate_limiter import GlobalRateLimiter
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
        allow_duplicate_ids: bool = False,
        on_progress: Callable[[int], None] | None = None,
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
    if data_type not in SUPPORTED_DATA_TYPES:
        raise BackfillValidationError(
            f"unsupported data_type {data_type!r}; supported: "
            f"{', '.join(sorted(SUPPORTED_DATA_TYPES))}"
        )
    if end < start:
        raise BackfillValidationError("start date must not be after end date")
    if (end - start).days + 1 > MAX_BACKFILL_DAYS:
        raise BackfillValidationError(f"backfill range exceeds the {MAX_BACKFILL_DAYS}-day limit")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_error_message(code: str) -> str:
    """Stable run-row messages; never persist filesystem/upstream detail."""
    if code == "BACKFILL_CHECKSUM_MISMATCH":
        return "archive checksum verification failed"
    if code == "VALIDATION_FAILED":
        return "archive rejected by the safety policy"
    if code == "DATA_UNAVAILABLE":
        return "required market data is unavailable"
    return "backfill failed"


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
        rate_limiter: GlobalRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._archive = archive
        self._parquet = parquet
        self._store = store
        self._clock = clock
        self._rate_limiter = rate_limiter
        self._progress_interval_seconds = 5.0

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
        owner = uuid.uuid4().hex
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
        if not self._store.start_backfill_run(record, owner=owner, now=started_at):
            raise BackfillAlreadyRunning("A backfill is already running.")
        self._reconcile_storage()

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
                    and self._resume_is_valid(existing, file)
                ):
                    verified += 1
                    processed += existing.bytes
                    self._store.resolve_data_gaps_for_date(
                        symbol=file.symbol,
                        data_type=file.data_type,
                        day=file.date.isoformat(),
                        resolved_at=utc_timestamp(self._clock.now()),
                    )
                    self._heartbeat(record, owner)
                    continue
                if existing is not None and existing.status == "COMPLETE":
                    structlog.get_logger(__name__).warning(
                        "backfill_manifest_stale",
                        symbol=file.symbol,
                        date=file.date.isoformat(),
                        reason="checksum or published part validation failed; reprocessing",
                    )
                try:
                    row = self._process_file(record, file, owner, existing)
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
                        message=_safe_error_message(exc.code),
                    )
                    self._save_run(finished, owner)
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
                        message=_safe_error_message(exc.code),
                    )
                    self._save_run(finished, owner)
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
                    self._heartbeat(record, owner)
                    continue
                verified += 1
                processed += row.bytes
                self._heartbeat(record, owner)
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
                message=_safe_error_message(exc.code),
            )
            self._save_run(finished, owner)
            raise
        except Exception:  # pragma: no cover - defensive run bookkeeping
            finished = replace(
                record,
                status="FAILED",
                finished_at=utc_timestamp(self._clock.now()),
                processed_bytes=processed,
                files_verified=verified,
                files_failed=failed,
                error_code="INTERNAL_ERROR",
                message="backfill failed",
            )
            self._save_run(finished, owner)
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
        self._save_run(final, owner)
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
        rows = self._store.list_backfill_manifest(
            symbol=symbol,
            data_type=data_type,
            start=start.isoformat(),
            end=end.isoformat(),
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

    def _download(
        self, file: ArchiveFile, run: BackfillRunRecord, owner: str
    ) -> tuple[Path, int, str]:
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
                    size = stream_to_file(
                        stream,
                        temp_path,
                        max_bytes=MAX_DOWNLOAD_BYTES,
                        on_progress=self._progress_callback(run, owner),
                    )
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

    def _process_file(
        self,
        run: BackfillRunRecord,
        file: ArchiveFile,
        owner: str,
        existing: BackfillManifestRow | None = None,
    ) -> BackfillManifestRow:
        zip_path, size, expected = self._download(file, run, owner)
        # A changed upstream checksum means the archive was corrected: the same
        # agg_trade_ids must be rewritten (and the old day parts replaced).
        corrected = (
            existing is not None and bool(existing.checksum) and existing.checksum != expected
        )
        extraction = extract_zip_safely(
            zip_path,
            workspace=self._work_dir(),
            on_progress=self._progress_callback(run, owner),
        )
        entries: tuple[ManifestEntry, ...] = ()
        try:
            csv_files = tuple(path for path in extraction.files if path.suffix.lower() == ".csv")
            if not csv_files:
                raise ArchiveError(f"archive contains no CSV file: {file.filename}")
            trades: list[AggTrade] = []
            for csv_path in csv_files:
                trades.extend(
                    extract_agg_trade_csv(
                        csv_path,
                        symbol=file.symbol,
                        on_progress=self._progress_callback(run, owner),
                    )
                )
            self._heartbeat(run, owner)  # dedupe/publish can also be long
            entries = self._parquet.write_agg_trades_batch(
                trades,
                symbol=file.symbol,
                source=f"archive:{file.url}",
                allow_duplicate_ids=corrected,
                on_progress=self._progress_callback(run, owner),
            )
            self._heartbeat(run, owner)
            day_parts = tuple(
                entry
                for entry in self._store.list_manifest(
                    symbol=file.symbol, date=file.date.isoformat()
                )
                if Path(entry.path).is_file()
            )
            row_rows = sum(entry.rows for entry in entries)
            if not corrected:
                # A manifest-bypassed rerun can be fully deduped; the day's
                # adopted/existing parts still prove the data is durable.
                row_rows = row_rows or sum(entry.rows for entry in day_parts)
            if row_rows == 0:
                raise ArchiveError(
                    f"archive produced no durable rows for {file.date.isoformat()}; "
                    "refusing to mark it COMPLETE"
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
                rows=row_rows,
                downloaded_at=now,
                verified_at=now,
                error_code=None,
            )
            if corrected:
                # Only archive-derived parts may be replaced; never touch
                # live/scan parts for the same symbol+date.
                new_paths = {entry.path for entry in entries}
                old_archive_parts = tuple(
                    entry
                    for entry in day_parts
                    if entry.path not in new_paths
                    and (entry.source.startswith("archive:") or entry.source == "recovered:orphan")
                )
            else:
                old_archive_parts = ()
            # Parts + gap resolve + COMPLETE in one transaction, COMPLETE last.
            self._store.commit_backfill_file(
                row=row,
                entries=entries,
                resolved_at=now,
                owner=owner,
                replace_date=corrected,
            )
            # Old parts are deleted only after the replacement transaction
            # committed; a failed commit leaves them and their manifest rows.
            for old in old_archive_parts:
                path = Path(old.path)
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:  # pragma: no cover - defensive cleanup
                    structlog.get_logger(__name__).warning(
                        "backfill_old_part_unlink_failed",
                        path=path.name,
                        error=str(exc),
                    )
                else:
                    _fsync_directory(path.parent)
            return row
        except Exception:
            # Downgrade/remove manifest rows first, then delete published parts.
            try:
                self._store.discard_backfill_file(
                    file_key=file_key(file.data_type, file.symbol, file.date),
                    entries=entries,
                    error_code="DATA_UNAVAILABLE",
                    owner=owner,
                    run_id=run.run_id,
                )
            finally:
                for entry in entries:
                    path = Path(entry.path)
                    path.unlink(missing_ok=True)
                    _fsync_directory(path.parent)
            raise
        finally:
            shutil.rmtree(extraction.directory, ignore_errors=True)

    def _save_run(self, record: BackfillRunRecord, owner: str) -> None:
        if not self._store.save_backfill_run(record, owner=owner):
            raise BackfillError("backfill lease lost; another run owns this job")

    def _heartbeat(self, run: BackfillRunRecord, owner: str) -> None:
        if not self._store.touch_backfill_run(
            run.run_id,
            updated_at=utc_timestamp(self._clock.now()),
            owner=owner,
        ):
            raise BackfillError("backfill lease lost; another run owns this job")

    def _progress_callback(self, run: BackfillRunRecord, owner: str) -> Callable[[int], None]:
        state: dict[str, datetime | None] = {"at": None}

        def progress(_written: int) -> None:
            # Time is injected; a deterministic clock never throttles by wall
            # time, so tests and replay stay reproducible.
            now = self._clock.now()
            previous = state["at"]
            elapsed = 0.0 if previous is None else (now - previous).total_seconds()
            if previous is None or elapsed >= self._progress_interval_seconds:
                state["at"] = now
                self._heartbeat(run, owner)

        return progress

    def _resume_is_valid(self, existing: BackfillManifestRow, file: ArchiveFile) -> bool:
        try:
            remote = parse_checksum_text(self._archive.read_checksum(file))[0]
        except ArchiveError:
            # Offline/unreachable source: trust local manifest + published parts
            # instead of reprocessing data we already hold.
            remote = None
        if remote is not None and remote != existing.checksum:
            return False
        parts = self._store.list_manifest(symbol=file.symbol, date=file.date.isoformat())
        return bool(parts) and all(Path(entry.path).is_file() for entry in parts)

    def _reconcile_storage(self) -> None:
        """Clean crash leftovers and adopt/repair part-manifest inconsistencies."""
        self._cleanup_stale_temp_files()
        known: set[str] = set()
        for entry in self._store.list_manifest():
            if Path(entry.path).is_file():
                known.add(entry.path)
            else:
                self._store.delete_manifest_entry(entry.path)
        for entry in recover_orphan_parts(self._settings, known):
            self._store.record_manifest(entry)

    def _cleanup_stale_temp_files(self) -> None:
        raw_root = self._settings.data_dir / "raw"
        if raw_root.is_dir():
            for path in raw_root.rglob("*.part"):
                path.unlink(missing_ok=True)
        extract_root = self._settings.data_dir / "run" / "extract"
        if extract_root.is_dir():
            for path in extract_root.glob("oft-extract-*"):
                shutil.rmtree(path, ignore_errors=True)

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
