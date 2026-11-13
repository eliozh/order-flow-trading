"""Backfill run, manifest, and data-gap mixin.

Covers the ``backfill_runs``, ``backfill_manifest``, ``manifest`` (part rows), and
``data_gaps`` tables plus their row mappers. Owns the backfill part-manifest rows
(``_insert_part_manifest``); the scan mixin owns the scan manifest rows.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.domain.data_quality import DataGap, GapSeverity
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    BackfillManifestRow,
    BackfillRunRecord,
    LeaseLostError,
    ManifestEntry,
    StoreUnavailableError,
)

STALE_RUNNING_SECONDS = 15 * 60


class BackfillStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write backfill runs, manifests, part rows, and data gaps."""

    def start_backfill_run(
        self,
        record: BackfillRunRecord,
        *,
        owner: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Insert a RUNNING run atomically; ``False`` if one is already running.

        A hard crash can leave ``RUNNING`` rows forever. Runs whose heartbeat is
        older than the lease window are marked ``STALE`` before the guard check.
        ``now`` lets the caller inject a ClockPort timestamp.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_dt = (
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                if now is not None
                else datetime.now(UTC)
            )
            stale_before = utc_timestamp(now_dt - timedelta(seconds=STALE_RUNNING_SECONDS))
            connection.execute(
                """
                UPDATE backfill_runs SET
                    status = 'STALE', finished_at = ?, error_code = ?,
                    message = 'stale RUNNING run recovered'
                WHERE status = 'RUNNING'
                  AND COALESCE(updated_at, started_at) < ?
                """,
                (utc_timestamp(now_dt), "INTERRUPTED", stale_before),
            )
            running = connection.execute(
                "SELECT 1 FROM backfill_runs WHERE status = 'RUNNING' LIMIT 1"
            ).fetchone()
            if running is not None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO backfill_runs (
                    run_id, symbol, data_type, start_date, end_date, status,
                    started_at, updated_at, owner, finished_at, estimated_bytes,
                    processed_bytes, files_total, files_verified, files_failed,
                    error_code, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.symbol,
                    record.data_type,
                    record.start_date,
                    record.end_date,
                    record.status,
                    record.started_at,
                    record.started_at,
                    owner,
                    record.finished_at,
                    record.estimated_bytes,
                    record.processed_bytes,
                    record.files_total,
                    record.files_verified,
                    record.files_failed,
                    record.error_code,
                    record.message,
                ),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill run insert failed: {exc}") from exc
        finally:
            connection.close()
        return True

    def touch_backfill_run(self, run_id: str, *, updated_at: str, owner: str | None = None) -> bool:
        """Record a heartbeat; ``False`` means the lease was lost (do not continue)."""
        connection = self._connect()
        try:
            if owner is None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING'",
                    (updated_at, run_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (updated_at, run_id, owner),
                )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def save_backfill_run(self, record: BackfillRunRecord, *, owner: str | None = None) -> bool:
        connection = self._connect()
        try:
            if owner is None:
                cursor = connection.execute(
                    """
                    UPDATE backfill_runs SET
                        status = ?, finished_at = ?, estimated_bytes = ?,
                        processed_bytes = ?, files_total = ?, files_verified = ?,
                        files_failed = ?, error_code = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (
                        record.status,
                        record.finished_at,
                        record.estimated_bytes,
                        record.processed_bytes,
                        record.files_total,
                        record.files_verified,
                        record.files_failed,
                        record.error_code,
                        record.message,
                        record.run_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE backfill_runs SET
                        status = ?, finished_at = ?, estimated_bytes = ?,
                        processed_bytes = ?, files_total = ?, files_verified = ?,
                        files_failed = ?, error_code = ?, message = ?
                    WHERE run_id = ? AND status = 'RUNNING' AND owner = ?
                    """,
                    (
                        record.status,
                        record.finished_at,
                        record.estimated_bytes,
                        record.processed_bytes,
                        record.files_total,
                        record.files_verified,
                        record.files_failed,
                        record.error_code,
                        record.message,
                        record.run_id,
                        owner,
                    ),
                )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def get_backfill_run(self, run_id: str) -> BackfillRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_run(row) if row is not None else None

    def latest_backfill_run(self) -> BackfillRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_run(row) if row is not None else None

    def list_backfill_runs(self, limit: int = 50) -> tuple[BackfillRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM backfill_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_backfill_run(row) for row in rows)

    def upsert_backfill_manifest(self, row: BackfillManifestRow) -> None:
        with closing(self._connect()) as connection, connection:
            self._upsert_backfill_manifest_row(connection, row)

    def _upsert_backfill_manifest_row(
        self, connection: sqlite3.Connection, row: BackfillManifestRow
    ) -> None:
        connection.execute(
            """
            INSERT INTO backfill_manifest (
                file_key, run_id, data_type, symbol, date, source_url, checksum,
                checksum_verified, status, bytes, rows, downloaded_at, verified_at,
                error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_key) DO UPDATE SET
                run_id = excluded.run_id,
                source_url = excluded.source_url,
                checksum = excluded.checksum,
                checksum_verified = excluded.checksum_verified,
                status = excluded.status,
                bytes = excluded.bytes,
                rows = excluded.rows,
                downloaded_at = excluded.downloaded_at,
                verified_at = excluded.verified_at,
                error_code = excluded.error_code
            """,
            (
                row.file_key,
                row.run_id,
                row.data_type,
                row.symbol,
                row.date,
                row.source_url,
                row.checksum,
                int(row.checksum_verified),
                row.status,
                row.bytes,
                row.rows,
                row.downloaded_at,
                row.verified_at,
                row.error_code,
            ),
        )

    def _insert_part_manifest(self, connection: sqlite3.Connection, entry: ManifestEntry) -> None:
        connection.execute(
            """
            INSERT INTO manifest (
                path, data_type, symbol, date, rows, bytes, checksum, source,
                created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                rows = excluded.rows,
                bytes = excluded.bytes,
                checksum = excluded.checksum,
                source = excluded.source,
                status = excluded.status
            """,
            (
                entry.path,
                entry.data_type,
                entry.symbol,
                entry.date,
                entry.rows,
                entry.bytes,
                entry.checksum,
                entry.source,
                entry.created_at,
                entry.status,
            ),
        )

    def _resolve_gaps_for_day(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        data_type: str,
        day: str,
        resolved_at: str,
        resolution: str = "BACKFILL_RESOLVED",
    ) -> None:
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        connection.execute(
            """
            UPDATE data_gaps SET resolved_at = ?, resolution = ?
            WHERE symbol = ? AND data_type = ?
              AND gap_start >= ? AND gap_start < ?
              AND resolved_at IS NULL
            """,
            (
                resolved_at,
                resolution,
                symbol,
                data_type,
                f"{day}T00:00:00.000Z",
                f"{next_day}T00:00:00.000Z",
            ),
        )

    def commit_backfill_file(
        self,
        *,
        row: BackfillManifestRow,
        entries: tuple[ManifestEntry, ...],
        resolved_at: str,
        owner: str | None = None,
        replace_date: bool = False,
    ) -> None:
        """Unit of work: part manifest + gap resolve + COMPLETE row, COMPLETE last.

        The COMPLETE row must never survive without its published parts; a
        failure rolls the whole transaction back so the caller can delete the
        parts and retry safely. When ``owner`` is supplied the run must still be
        ``RUNNING`` under that owner, otherwise ``LeaseLostError`` aborts.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if owner is not None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (resolved_at, row.run_id, owner),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise LeaseLostError()
            for entry in entries:
                self._insert_part_manifest(connection, entry)
            if replace_date:
                paths = [entry.path for entry in entries]
                if paths:
                    placeholders = ", ".join("?" for _ in paths)
                    connection.execute(
                        f"DELETE FROM manifest WHERE symbol = ? AND date = ? "
                        f"AND (source LIKE 'archive:%' OR source = 'recovered:orphan') "
                        f"AND path NOT IN ({placeholders})",
                        (row.symbol, row.date, *paths),
                    )
                else:
                    connection.execute(
                        "DELETE FROM manifest WHERE symbol = ? AND date = ? "
                        "AND (source LIKE 'archive:%' OR source = 'recovered:orphan')",
                        (row.symbol, row.date),
                    )
            self._resolve_gaps_for_day(
                connection,
                symbol=row.symbol,
                data_type=row.data_type,
                day=row.date,
                resolved_at=resolved_at,
            )
            self._upsert_backfill_manifest_row(connection, row)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill file commit failed: {exc}") from exc
        finally:
            connection.close()

    def discard_backfill_file(
        self,
        *,
        file_key: str,
        entries: tuple[ManifestEntry, ...],
        error_code: str,
        owner: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Downgrade a published COMPLETE row and drop its part rows before cleanup."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if owner is not None and run_id is not None:
                cursor = connection.execute(
                    "UPDATE backfill_runs SET updated_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING' AND owner = ?",
                    (utc_timestamp(datetime.now(UTC)), run_id, owner),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise LeaseLostError()
            for entry in entries:
                connection.execute("DELETE FROM manifest WHERE path = ?", (entry.path,))
            if run_id is None:
                connection.execute(
                    """
                    UPDATE backfill_manifest SET
                        status = 'FAILED', checksum_verified = 0, error_code = ?
                    WHERE file_key = ? AND status = 'COMPLETE'
                    """,
                    (error_code, file_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE backfill_manifest SET
                        status = 'FAILED', checksum_verified = 0, error_code = ?
                    WHERE file_key = ? AND run_id = ? AND status = 'COMPLETE'
                    """,
                    (error_code, file_key, run_id),
                )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"backfill file discard failed: {exc}") from exc
        finally:
            connection.close()

    def get_backfill_manifest(self, file_key: str) -> BackfillManifestRow | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM backfill_manifest WHERE file_key = ?", (file_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_backfill_manifest(row) if row is not None else None

    def list_backfill_manifest(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        run_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[BackfillManifestRow, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if data_type is not None:
            clauses.append("data_type = ?")
            params.append(data_type)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if start is not None:
            clauses.append("date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("date <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM backfill_manifest{where} ORDER BY date, file_key", params
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_backfill_manifest(row) for row in rows)

    def upsert_data_gap(self, gap: DataGap) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO data_gaps (
                    gap_id, symbol, data_type, gap_start, gap_end, reason, severity,
                    detected_at, source, resolved_at, resolution, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gap_id) DO UPDATE SET
                    gap_start = excluded.gap_start,
                    gap_end = excluded.gap_end,
                    reason = excluded.reason,
                    severity = excluded.severity,
                    detected_at = excluded.detected_at,
                    source = excluded.source,
                    resolved_at = excluded.resolved_at,
                    resolution = excluded.resolution,
                    run_id = excluded.run_id
                """,
                (
                    gap.gap_id,
                    gap.symbol,
                    gap.data_type,
                    gap.start,
                    gap.end,
                    gap.reason,
                    gap.severity.value,
                    gap.detected_at,
                    gap.source,
                    gap.resolved_at,
                    gap.resolution,
                    gap.run_id,
                ),
            )

    def resolve_data_gaps_for_date(
        self,
        *,
        symbol: str,
        data_type: str,
        day: str,
        resolved_at: str,
        resolution: str = "BACKFILL_RESOLVED",
    ) -> None:
        """Mark gaps covering a now-verified/published day as resolved."""
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE data_gaps SET resolved_at = ?, resolution = ?
                WHERE symbol = ? AND data_type = ?
                  AND gap_start >= ? AND gap_start < ?
                  AND resolved_at IS NULL
                """,
                (
                    resolved_at,
                    resolution,
                    symbol,
                    data_type,
                    f"{day}T00:00:00.000Z",
                    f"{next_day}T00:00:00.000Z",
                ),
            )

    def list_data_gaps(
        self,
        *,
        symbol: str | None = None,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[DataGap, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if data_type is not None:
            clauses.append("data_type = ?")
            params.append(data_type)
        if start is not None:
            clauses.append("gap_start >= ?")
            params.append(f"{start}T00:00:00.000Z")
        if end is not None:
            # Half-open: the end day itself is included, the next day is not.
            end_exclusive = (datetime.fromisoformat(end).date() + timedelta(days=1)).isoformat()
            clauses.append("gap_start < ?")
            params.append(f"{end_exclusive}T00:00:00.000Z")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM data_gaps{where} ORDER BY gap_start, gap_id", params
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_data_gap(row) for row in rows)


def _row_to_backfill_run(row: sqlite3.Row) -> BackfillRunRecord:
    return BackfillRunRecord(
        run_id=row["run_id"],
        symbol=row["symbol"],
        data_type=row["data_type"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        estimated_bytes=row["estimated_bytes"],
        processed_bytes=row["processed_bytes"],
        files_total=row["files_total"],
        files_verified=row["files_verified"],
        files_failed=row["files_failed"],
        error_code=row["error_code"],
        message=row["message"],
    )


def _row_to_backfill_manifest(row: sqlite3.Row) -> BackfillManifestRow:
    return BackfillManifestRow(
        file_key=row["file_key"],
        run_id=row["run_id"],
        data_type=row["data_type"],
        symbol=row["symbol"],
        date=row["date"],
        source_url=row["source_url"],
        checksum=row["checksum"],
        checksum_verified=bool(row["checksum_verified"]),
        status=row["status"],
        bytes=row["bytes"],
        rows=row["rows"],
        downloaded_at=row["downloaded_at"],
        verified_at=row["verified_at"],
        error_code=row["error_code"],
    )


def _row_to_data_gap(row: sqlite3.Row) -> DataGap:
    return DataGap(
        gap_id=row["gap_id"],
        symbol=row["symbol"],
        data_type=row["data_type"],
        start=row["gap_start"],
        end=row["gap_end"],
        reason=row["reason"],
        severity=GapSeverity(row["severity"]),
        detected_at=row["detected_at"],
        source=row["source"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        run_id=row["run_id"],
    )
