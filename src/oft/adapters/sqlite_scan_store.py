"""Scan, report, idempotency, and manifest mixin.

Covers the ``scans``, ``reports``, ``idempotency_keys``, ``manifest``, and
``scan_symbols`` tables plus their row mappers. Owns the scan manifest rows
(``_insert_manifest``); the backfill mixin owns the backfill part-manifest rows.
Bounded symbol report pages, counts, and per-timeframe headers live in
``sqlite_report_reads.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timedelta

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    IdempotencyRecord,
    ManifestEntry,
    ReportRecord,
    ScanRecord,
    ScanSymbolRecord,
    StoreUnavailableError,
)

_LOGGER = logging.getLogger(__name__)
# One report-llm read never asks SQLite for more than this many placeholders.
_REPORT_LLM_CHUNK = 500


class ScanStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write scans, reports, idempotency keys, and manifest rows."""

    def save_scan(self, record: ScanRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_scan(connection, record)

    def _insert_scan(self, connection: sqlite3.Connection, record: ScanRecord) -> None:
        connection.execute(
            """
            INSERT INTO scans (
                scan_id, symbol, period, status, trigger_source, metrics_only,
                fixture_path, started_at, finished_at, window_start, window_end,
                trade_count, buy_volume, sell_volume, delta, cvd, data_quality,
                manifest_path, parquet_rows, created_at, report_id, source_mode,
                metrics_manifest_json, error_code, error_message, batch_id,
                llm_configured, llm_status, llm_tokens, cost_usd, cost_unpriced
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                record.scan_id,
                record.symbol,
                record.period,
                record.status,
                record.trigger_source,
                int(record.metrics_only),
                record.fixture_path,
                record.started_at,
                record.finished_at,
                record.window_start,
                record.window_end,
                record.trade_count,
                record.buy_volume,
                record.sell_volume,
                record.delta,
                record.cvd,
                record.data_quality,
                record.manifest_path,
                record.parquet_rows,
                record.created_at,
                record.report_id,
                record.source_mode,
                record.metrics_manifest_json,
                record.error_code,
                record.error_message,
                record.batch_id,
                int(record.llm_configured),
                record.llm_status,
                record.llm_tokens,
                record.cost_usd,
                int(record.cost_unpriced),
            ),
        )

    def update_scan(self, record: ScanRecord) -> None:
        """Update the mutable lifecycle fields of an existing scan row."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE scans SET
                    status = ?, finished_at = ?, data_quality = ?, report_id = ?,
                    trade_count = ?, buy_volume = ?, sell_volume = ?, delta = ?,
                    cvd = ?, manifest_path = ?, parquet_rows = ?,
                    metrics_manifest_json = ?, error_code = ?, error_message = ?,
                    batch_id = ?, llm_configured = ?, llm_status = ?,
                    llm_tokens = ?, cost_usd = ?, cost_unpriced = ?
                WHERE scan_id = ?
                """,
                (
                    record.status,
                    record.finished_at,
                    record.data_quality,
                    record.report_id,
                    record.trade_count,
                    record.buy_volume,
                    record.sell_volume,
                    record.delta,
                    record.cvd,
                    record.manifest_path,
                    record.parquet_rows,
                    record.metrics_manifest_json,
                    record.error_code,
                    record.error_message,
                    record.batch_id,
                    int(record.llm_configured),
                    record.llm_status,
                    record.llm_tokens,
                    record.cost_usd,
                    int(record.cost_unpriced),
                    record.scan_id,
                ),
            )

    def save_scan_with_manifest(self, record: ScanRecord, entry: ManifestEntry) -> None:
        """Persist the scan row and manifest row in one transaction (unit of work)."""
        with closing(self._connect()) as connection, connection:
            self._insert_scan(connection, record)
            self._insert_manifest(connection, entry)

    def get_scan(self, scan_id: str) -> ScanRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        finally:
            connection.close()
        return _row_to_scan(row) if row is not None else None

    def latest_scan(self) -> ScanRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM scans ORDER BY created_at DESC, scan_id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_scan(row) if row is not None else None

    def list_scans(self, limit: int = 20, offset: int = 0) -> tuple[ScanRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scans ORDER BY created_at DESC, scan_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan(row) for row in rows)

    def list_scans_by_batch(self, batch_id: str) -> tuple[ScanRecord, ...]:
        """Return every child scan of one batch, ordered by symbol.

        Uses the ``scans(batch_id)`` index so a batch whose rows are older than
        the newest page of scans still renders its full window and quality.
        """
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scans WHERE batch_id = ? ORDER BY symbol", (batch_id,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan(row) for row in rows)

    def count_scans(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) FROM scans").fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def save_report(self, record: ReportRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_report(connection, record)

    def _insert_report(self, connection: sqlite3.Connection, record: ReportRecord) -> None:
        connection.execute(
            """
            INSERT INTO reports (
                report_id, scan_id, symbol, timeframe, generated_at,
                metrics_manifest_json, llm_decision_id, narrative_ref,
                data_quality, snapshot_json, metrics_json, llm_json,
                window_policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.report_id,
                record.scan_id,
                record.symbol,
                record.timeframe,
                record.generated_at,
                record.metrics_manifest_json,
                record.llm_decision_id,
                record.narrative_ref,
                record.data_quality,
                record.snapshot_json,
                record.metrics_json,
                record.llm_json,
                record.window_policy_json,
            ),
        )

    def save_report_and_complete_scan(self, report: ReportRecord, scan: ScanRecord) -> None:
        """Insert the report and finish the scan in one transaction.

        A failure in either statement rolls both back, so a report can never be
        committed without the matching DONE scan row.
        """
        with closing(self._connect()) as connection, connection:
            self._insert_report(connection, report)
            cursor = connection.execute(
                """
                UPDATE scans SET
                    status = ?, finished_at = ?, data_quality = ?, report_id = ?,
                    trade_count = ?, buy_volume = ?, sell_volume = ?, delta = ?,
                    cvd = ?, manifest_path = ?, parquet_rows = ?,
                    metrics_manifest_json = ?, error_code = ?, error_message = ?,
                    batch_id = ?, llm_configured = ?, llm_status = ?,
                    llm_tokens = ?, cost_usd = ?, cost_unpriced = ?
                WHERE scan_id = ?
                """,
                (
                    scan.status,
                    scan.finished_at,
                    scan.data_quality,
                    scan.report_id,
                    scan.trade_count,
                    scan.buy_volume,
                    scan.sell_volume,
                    scan.delta,
                    scan.cvd,
                    scan.manifest_path,
                    scan.parquet_rows,
                    scan.metrics_manifest_json,
                    scan.error_code,
                    scan.error_message,
                    scan.batch_id,
                    int(scan.llm_configured),
                    scan.llm_status,
                    scan.llm_tokens,
                    scan.cost_usd,
                    int(scan.cost_unpriced),
                    scan.scan_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreUnavailableError(
                    f"scan {scan.scan_id} disappeared before report completion"
                )

    def fail_stale_scans(self, *, now: str, older_than_seconds: int = 900) -> int:
        """Mark RUNNING scans older than the lease window as FAILED."""
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        stale_before = utc_timestamp(now_dt - timedelta(seconds=older_than_seconds))
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE scans SET
                    status = 'FAILED', finished_at = ?, data_quality = 'MISSING',
                    error_code = 'SCAN_STALE',
                    error_message = 'scan exceeded the RUNNING lease and was failed'
                WHERE status = 'RUNNING' AND started_at < ?
                """,
                (now, stale_before),
            )
        return cursor.rowcount

    def get_report(self, report_id: str) -> ReportRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_report(row) if row is not None else None

    def list_report_llm_json(self, report_ids: Sequence[str]) -> tuple[str, ...]:
        """The non-empty ``llm_json`` column for the given report ids (chunked)."""
        unique_ids = tuple(
            dict.fromkeys(
                report_id for report_id in report_ids if isinstance(report_id, str) and report_id
            )
        )
        if not unique_ids:
            return ()
        connection = self._connect_read_only()
        if connection is None:
            return ()
        values: list[str] = []
        try:
            for start in range(0, len(unique_ids), _REPORT_LLM_CHUNK):
                chunk = unique_ids[start : start + _REPORT_LLM_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT llm_json FROM reports WHERE report_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                values.extend(row[0] for row in rows if isinstance(row[0], str) and row[0])
        finally:
            connection.close()
        return tuple(values)

    def list_reports(
        self, *, scan_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[ReportRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where = " WHERE scan_id = ?" if scan_id is not None else ""
        params: tuple[object, ...] = (
            (scan_id, limit, offset) if scan_id is not None else (limit, offset)
        )
        try:
            rows = connection.execute(
                f"SELECT * FROM reports{where} "
                "ORDER BY generated_at DESC, report_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_report(row) for row in rows)

    def count_reports(self, *, scan_id: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where = " WHERE scan_id = ?" if scan_id is not None else ""
        params: tuple[object, ...] = (scan_id,) if scan_id is not None else ()
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM reports{where}", params).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def get_idempotency(self, *, scope: str, key: str) -> IdempotencyRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_idempotency(row) if row is not None else None

    def reserve_idempotency(self, record: IdempotencyRecord) -> bool:
        """Atomically reserve a key; False means the key already exists."""
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, key, request_hash, response_status, response_json,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                        request_hash = excluded.request_hash,
                        response_status = excluded.response_status,
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    WHERE idempotency_keys.expires_at <= excluded.created_at
                    """,
                    (
                        record.scope,
                        record.key,
                        record.request_hash,
                        record.response_status,
                        record.response_json,
                        record.created_at,
                        record.expires_at,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return cursor.rowcount == 1

    def renew_idempotency(self, *, scope: str, key: str, expires_at: str) -> None:
        """Extend an in-flight reservation lease without touching its payload."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE idempotency_keys SET expires_at = ? "
                "WHERE scope = ? AND key = ? AND response_json = ''",
                (expires_at, scope, key),
            )

    def complete_idempotency(self, record: IdempotencyRecord) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE idempotency_keys SET
                    request_hash = ?, response_status = ?, response_json = ?,
                    created_at = ?, expires_at = ?
                WHERE scope = ? AND key = ?
                """,
                (
                    record.request_hash,
                    record.response_status,
                    record.response_json,
                    record.created_at,
                    record.expires_at,
                    record.scope,
                    record.key,
                ),
            )
            if cursor.rowcount != 1:
                # The lease expired or was purged while the request ran. Re-insert
                # the completed record so the response is not silently lost; the
                # replay path must still be able to return it.
                _LOGGER.warning(
                    "idempotency lease for %s/%s vanished before completion; re-inserting",
                    record.scope,
                    record.key,
                )
                connection.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, key, request_hash, response_status, response_json,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                        request_hash = excluded.request_hash,
                        response_status = excluded.response_status,
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        record.scope,
                        record.key,
                        record.request_hash,
                        record.response_status,
                        record.response_json,
                        record.created_at,
                        record.expires_at,
                    ),
                )

    def delete_idempotency(self, *, scope: str, key: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key)
            )

    def purge_expired_idempotency(self, *, now: str) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= ?", (now,)
            )
        return cursor.rowcount

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys (
                    scope, key, request_hash, response_status, response_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.scope,
                    record.key,
                    record.request_hash,
                    record.response_status,
                    record.response_json,
                    record.created_at,
                    record.expires_at,
                ),
            )

    def record_manifest(self, entry: ManifestEntry) -> None:
        # Scans/backfills are append-only: a duplicate path is a real conflict.
        with closing(self._connect()) as connection, connection:
            self._insert_manifest(connection, entry)

    def upsert_manifest_entry(self, entry: ManifestEntry) -> None:
        """Metrics-only idempotent manifest write (re-persist same content)."""
        try:
            with closing(self._connect()) as connection, connection:
                self._insert_manifest(connection, entry, upsert=True)
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest write failed: {exc}") from exc

    def upsert_manifest_entries(self, entries: tuple[ManifestEntry, ...]) -> None:
        """Publish metrics + footprint manifest rows in one transaction."""
        try:
            with closing(self._connect()) as connection, connection:
                for entry in entries:
                    self._insert_manifest(connection, entry, upsert=True)
        except sqlite3.Error as exc:
            # "database is locked"/disk-full are OperationalError, not OSError;
            # map them so the supervisor treats them as fatal.
            raise StoreUnavailableError(f"manifest write failed: {exc}") from exc

    def _insert_manifest(
        self, connection: sqlite3.Connection, entry: ManifestEntry, *, upsert: bool = False
    ) -> None:
        conflict = "ON CONFLICT(path) DO NOTHING" if upsert else ""
        connection.execute(
            f"""
            INSERT INTO manifest (
                path, data_type, symbol, date, rows, bytes, checksum,
                source, created_at, status, metric_version, input_manifest_hash,
                window_start, window_end, run_id, last_n, recomputed_from,
                parent_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            {conflict}
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
                entry.metric_version,
                entry.input_manifest_hash,
                entry.window_start,
                entry.window_end,
                entry.run_id,
                entry.last_n,
                entry.recomputed_from,
                entry.parent_run_id,
            ),
        )

    def delete_manifest_entry(self, path: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM manifest WHERE path = ?", (path,))

    def get_manifest_entry(self, path: str) -> ManifestEntry | None:
        """Return one manifest row; raises when the DB itself is unreadable."""
        connection = self._connect_read_only()
        if connection is None:
            raise StoreUnavailableError("manifest database is unreadable")
        try:
            row = connection.execute(
                "SELECT rowid AS run_seq, * FROM manifest WHERE path = ?", (path,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest lookup failed: {exc}") from exc
        finally:
            connection.close()
        return _row_to_manifest(row) if row is not None else None

    def list_manifest(
        self, *, symbol: str | None = None, date: str | None = None
    ) -> tuple[ManifestEntry, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if date is not None:
            clauses.append("date = ?")
            params.append(date)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            connection = self._connect_read_only()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest database is unreadable: {exc}") from exc
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT rowid AS run_seq, * FROM manifest{where} ORDER BY created_at, path",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"manifest read failed: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_manifest(row) for row in rows)

    def upsert_scan_symbol(self, record: ScanSymbolRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO scan_symbols (
                    scan_id, symbol, stage, started_at, finished_at, error_code,
                    error_message, failed_stage, report_id, data_quality, llm_status,
                    llm_tokens, cost_usd, cost_unpriced
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id, symbol) DO UPDATE SET
                    stage = excluded.stage,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    failed_stage = excluded.failed_stage,
                    report_id = excluded.report_id,
                    data_quality = excluded.data_quality,
                    llm_status = excluded.llm_status,
                    llm_tokens = excluded.llm_tokens,
                    cost_usd = excluded.cost_usd,
                    cost_unpriced = excluded.cost_unpriced
                """,
                (
                    record.scan_id,
                    record.symbol,
                    record.stage,
                    record.started_at,
                    record.finished_at,
                    record.error_code,
                    record.error_message,
                    record.failed_stage,
                    record.report_id,
                    record.data_quality,
                    record.llm_status,
                    record.llm_tokens,
                    record.cost_usd,
                    int(record.cost_unpriced),
                ),
            )

    def list_scan_symbols(self, scan_id: str) -> tuple[ScanSymbolRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM scan_symbols WHERE scan_id = ? ORDER BY symbol", (scan_id,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_scan_symbol(row) for row in rows)


def _row_to_scan(row: sqlite3.Row) -> ScanRecord:
    keys = row.keys()
    return ScanRecord(
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        period=row["period"],
        status=row["status"],
        trigger_source=row["trigger_source"],
        metrics_only=bool(row["metrics_only"]),
        fixture_path=row["fixture_path"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        trade_count=row["trade_count"],
        buy_volume=row["buy_volume"],
        sell_volume=row["sell_volume"],
        delta=row["delta"],
        cvd=row["cvd"],
        data_quality=row["data_quality"],
        manifest_path=row["manifest_path"],
        parquet_rows=row["parquet_rows"],
        created_at=row["created_at"],
        report_id=row["report_id"] if "report_id" in keys else None,
        source_mode=row["source_mode"] if "source_mode" in keys else "FIXTURE",
        metrics_manifest_json=(
            row["metrics_manifest_json"] if "metrics_manifest_json" in keys else None
        ),
        error_code=row["error_code"] if "error_code" in keys else None,
        error_message=row["error_message"] if "error_message" in keys else None,
        batch_id=row["batch_id"] if "batch_id" in keys else None,
        llm_configured=(bool(row["llm_configured"]) if "llm_configured" in keys else False),
        llm_status=row["llm_status"] if "llm_status" in keys else None,
        llm_tokens=int(row["llm_tokens"]) if "llm_tokens" in keys else 0,
        cost_usd=row["cost_usd"] if "cost_usd" in keys else "0",
        cost_unpriced=bool(row["cost_unpriced"]) if "cost_unpriced" in keys else True,
    )


def _row_to_report(row: sqlite3.Row) -> ReportRecord:
    keys = row.keys()
    return ReportRecord(
        report_id=row["report_id"],
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        generated_at=row["generated_at"],
        metrics_manifest_json=row["metrics_manifest_json"],
        llm_decision_id=row["llm_decision_id"],
        narrative_ref=row["narrative_ref"],
        data_quality=row["data_quality"],
        snapshot_json=row["snapshot_json"],
        metrics_json=row["metrics_json"],
        llm_json=row["llm_json"] if "llm_json" in keys else None,
        window_policy_json=(row["window_policy_json"] if "window_policy_json" in keys else None),
    )


def _row_to_idempotency(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        scope=row["scope"],
        key=row["key"],
        request_hash=row["request_hash"],
        response_status=row["response_status"],
        response_json=row["response_json"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _row_to_manifest(row: sqlite3.Row) -> ManifestEntry:
    return ManifestEntry(
        path=row["path"],
        data_type=row["data_type"],
        symbol=row["symbol"],
        date=row["date"],
        rows=row["rows"],
        bytes=row["bytes"],
        checksum=row["checksum"],
        source=row["source"],
        created_at=row["created_at"],
        status=row["status"],
        metric_version=row["metric_version"],
        input_manifest_hash=row["input_manifest_hash"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        run_id=row["run_id"],
        last_n=row["last_n"],
        recomputed_from=row["recomputed_from"],
        parent_run_id=row["parent_run_id"],
        run_seq=row["run_seq"],
    )


def _row_to_scan_symbol(row: sqlite3.Row) -> ScanSymbolRecord:
    keys = row.keys()
    return ScanSymbolRecord(
        scan_id=row["scan_id"],
        symbol=row["symbol"],
        stage=row["stage"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        report_id=row["report_id"],
        data_quality=row["data_quality"],
        error_message=row["error_message"] if "error_message" in keys else None,
        failed_stage=row["failed_stage"] if "failed_stage" in keys else None,
        llm_status=row["llm_status"] if "llm_status" in keys else None,
        llm_tokens=int(row["llm_tokens"]) if "llm_tokens" in keys else 0,
        cost_usd=row["cost_usd"] if "cost_usd" in keys else "0",
        cost_unpriced=bool(row["cost_unpriced"]) if "cost_unpriced" in keys else True,
    )
