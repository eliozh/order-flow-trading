"""Durable job-ledger mixin.

Covers the ``job_runs`` and ``job_locks`` tables plus their row mappers.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.ports.store import JobLockRecord, JobRunRecord, StoreUnavailableError


class JobStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write job runs, leases, and named locks."""

    def claim_job_run(self, record: JobRunRecord) -> tuple[bool, JobRunRecord | None, bool]:
        """Atomically claim ``(job_type, window_start)`` or reclaim a stale lease.

        Returns ``(claimed, run, reclaimed)``. A terminal row (``DONE``,
        ``FAILED``, ``MISSED``, ``SKIPPED``) is never replayed; a ``RUNNING``
        row is reclaimed only once its lease has expired, keeping the same
        ``run_id`` so checkpointed children can resume.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                (record.job_type, record.window_start),
            ).fetchone()
            if row is not None:
                existing = _row_to_job_run(row)
                stale = existing.lease_expires_at <= record.heartbeat_at
                if existing.status == "RUNNING" and stale:
                    connection.execute(
                        """
                        UPDATE job_runs SET
                            status = 'RUNNING', trigger_source = ?, started_at = ?,
                            finished_at = NULL, error_code = NULL, message = NULL,
                            lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
                            period = COALESCE(period, ?)
                        WHERE run_id = ?
                        """,
                        (
                            record.trigger_source,
                            record.started_at,
                            record.lease_owner,
                            record.lease_expires_at,
                            record.heartbeat_at,
                            record.period,
                            existing.run_id,
                        ),
                    )
                    updated = connection.execute(
                        "SELECT * FROM job_runs WHERE run_id = ?", (existing.run_id,)
                    ).fetchone()
                    connection.commit()
                    return True, _row_to_job_run(updated), True
                connection.commit()
                return False, existing, False
            try:
                connection.execute(
                    """
                    INSERT INTO job_runs (
                        run_id, job_type, window_start, status, trigger_source,
                        started_at, finished_at, error_code, message, lease_owner,
                        lease_expires_at, heartbeat_at, source, metrics_only, provider,
                        symbol, period
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.job_type,
                        record.window_start,
                        record.status,
                        record.trigger_source,
                        record.started_at,
                        record.finished_at,
                        record.error_code,
                        record.message,
                        record.lease_owner,
                        record.lease_expires_at,
                        record.heartbeat_at,
                        record.source,
                        int(record.metrics_only),
                        record.provider,
                        record.symbol,
                        record.period,
                    ),
                )
                connection.commit()
                return True, record, False
            except sqlite3.IntegrityError:
                connection.rollback()
                duplicate = connection.execute(
                    "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                    (record.job_type, record.window_start),
                ).fetchone()
                return False, _row_to_job_run(duplicate) if duplicate is not None else None, False
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailableError(f"job claim failed: {exc}") from exc
        finally:
            connection.close()

    def finish_job_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: str,
        error_code: str | None = None,
        message: str | None = None,
        lease_owner: str | None = None,
    ) -> bool:
        owner_clause = " AND lease_owner = ?" if lease_owner is not None else ""
        params: list[object] = [status, finished_at, error_code, message, run_id]
        if lease_owner is not None:
            params.append(lease_owner)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"""
                UPDATE job_runs SET status = ?, finished_at = ?, error_code = ?, message = ?
                WHERE run_id = ? AND status = 'RUNNING'{owner_clause}
                """,
                tuple(params),
            )
        return cursor.rowcount > 0

    def fail_stale_job_runs(self, *, job_type: str, now: str) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE job_runs SET
                    status = 'FAILED', finished_at = ?, error_code = 'LEASE_EXPIRED',
                    message = 'stale RUNNING scan lease expired'
                WHERE job_type = ? AND status = 'RUNNING' AND lease_expires_at <= ?
                """,
                (now, job_type, now),
            )
        return cursor.rowcount

    def expire_job_leases(self, *, job_type: str, now: str) -> int:
        """Expire every RUNNING lease without finishing the row.

        The row stays ``RUNNING`` with the same ``run_id`` so the normal claim
        path reclaims it (idempotently replaying its work). This is the startup
        recovery for a job whose restart catch-up must run, unlike
        :meth:`fail_stale_job_runs` which marks the row terminally ``FAILED``.
        """
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE job_runs SET lease_expires_at = ?
                WHERE job_type = ? AND status = 'RUNNING'
                """,
                (now, job_type),
            )
        return cursor.rowcount

    def delete_job_run(self, run_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM job_runs WHERE run_id = ? AND status = 'RUNNING'", (run_id,)
            )
        return cursor.rowcount > 0

    def renew_job_lease(
        self, run_id: str, *, owner: str, heartbeat_at: str, lease_expires_at: str
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE job_runs SET heartbeat_at = ?, lease_expires_at = ?
                WHERE run_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (heartbeat_at, lease_expires_at, run_id, owner),
            )
        return cursor.rowcount > 0

    def get_job_run(self, run_id: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def get_job_run_by_window(self, job_type: str, window_start: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_type = ? AND window_start = ?",
                (job_type, window_start),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def latest_job_run(self, job_type: str) -> JobRunRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT * FROM job_runs WHERE job_type = ?
                ORDER BY heartbeat_at DESC, rowid DESC LIMIT 1
                """,
                (job_type,),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_run(row) if row is not None else None

    def list_job_runs(
        self, job_type: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[JobRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where = " WHERE job_type = ?" if job_type is not None else ""
        params: tuple[object, ...] = (
            (job_type, limit, offset) if job_type is not None else (limit, offset)
        )
        try:
            rows = connection.execute(
                f"SELECT * FROM job_runs{where} ORDER BY heartbeat_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_job_run(row) for row in rows)

    def count_job_runs(self, job_type: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where = " WHERE job_type = ?" if job_type is not None else ""
        params: tuple[object, ...] = (job_type,) if job_type is not None else ()
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM job_runs{where}", params).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def list_stale_job_runs(self, *, now: str) -> tuple[JobRunRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM job_runs "
                "WHERE status = 'RUNNING' AND lease_expires_at <= ? "
                "ORDER BY heartbeat_at DESC, rowid DESC",
                (now,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_job_run(row) for row in rows)

    def acquire_job_lock(
        self, lock_name: str, owner: str, *, acquired_at: str, expires_at: str
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO job_locks (lock_name, owner, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                WHERE job_locks.expires_at <= excluded.acquired_at
                   OR job_locks.owner = excluded.owner
                """,
                (lock_name, owner, acquired_at, expires_at),
            )
        return cursor.rowcount == 1

    def release_job_lock(self, lock_name: str, owner: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM job_locks WHERE lock_name = ? AND owner = ?",
                (lock_name, owner),
            )
        return cursor.rowcount > 0

    def get_job_lock(self, lock_name: str) -> JobLockRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM job_locks WHERE lock_name = ?", (lock_name,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job_lock(row) if row is not None else None


def _row_to_job_run(row: sqlite3.Row) -> JobRunRecord:
    keys = row.keys()
    return JobRunRecord(
        run_id=row["run_id"],
        job_type=row["job_type"],
        window_start=row["window_start"],
        status=row["status"],
        trigger_source=row["trigger_source"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        message=row["message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        source=row["source"] if "source" in keys else "backfill",
        metrics_only=(bool(row["metrics_only"]) if "metrics_only" in keys else False),
        provider=row["provider"] if "provider" in keys else None,
        symbol=row["symbol"] if "symbol" in keys else None,
        period=row["period"] if "period" in keys else None,
    )


def _row_to_job_lock(row: sqlite3.Row) -> JobLockRecord:
    return JobLockRecord(
        lock_name=row["lock_name"],
        owner=row["owner"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )
