"""Write-only secret metadata (``secrets_meta``) with atomic audit writes.

The plaintext key lives only in the OS keyring or the encrypted-file backend.
SQLite stores the provider/name, a SHA-256 fingerprint, the last four
characters, and provenance so a surface can prove a key exists and detect a
rotation without ever holding the value.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from oft.ports.store import (
    AuditEventDraft,
    AuditEventRecord,
    SecretChangePlan,
    SecretMetaRecord,
)

if TYPE_CHECKING:
    pass


class SecretStoreMixin:
    """Read and write ``secrets_meta`` rows inside one transaction."""

    _connect: Callable[[], sqlite3.Connection]
    _connect_read_only: Callable[[], sqlite3.Connection | None]
    _insert_audit_event: Callable[[sqlite3.Connection, AuditEventDraft], AuditEventRecord]

    def get_secret_meta(self, provider: str, name: str = "api_key") -> SecretMetaRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT provider, name, fingerprint, last4, updated_at, updated_by, backend
                FROM secrets_meta WHERE provider = ? AND name = ?
                """,
                (provider, name),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_secrets_table(exc):
                return None
            if not _missing_backend_column(exc):
                raise
            # A pre-v29 database still serves reads before it is migrated.
            try:
                row = connection.execute(
                    """
                    SELECT provider, name, fingerprint, last4, updated_at, updated_by
                    FROM secrets_meta WHERE provider = ? AND name = ?
                    """,
                    (provider, name),
                ).fetchone()
            except sqlite3.OperationalError as fallback_exc:
                if _missing_secrets_table(fallback_exc):
                    return None
                raise
        finally:
            connection.close()
        return _row_to_secret_meta(row) if row is not None else None

    def list_secret_meta(self) -> tuple[SecretMetaRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                """
                SELECT provider, name, fingerprint, last4, updated_at, updated_by, backend
                FROM secrets_meta ORDER BY provider ASC, name ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_secrets_table(exc):
                return ()
            if not _missing_backend_column(exc):
                raise
            try:
                rows = connection.execute(
                    """
                    SELECT provider, name, fingerprint, last4, updated_at, updated_by
                    FROM secrets_meta ORDER BY provider ASC, name ASC
                    """
                ).fetchall()
            except sqlite3.OperationalError as fallback_exc:
                if _missing_secrets_table(fallback_exc):
                    return ()
                raise
        finally:
            connection.close()
        return tuple(_row_to_secret_meta(row) for row in rows)

    def save_secret_change(
        self,
        provider: str,
        name: str,
        build: Callable[[SecretMetaRecord | None], SecretChangePlan | None],
    ) -> tuple[SecretMetaRecord | None, AuditEventRecord | None]:
        """Read, decide, and write one secret-metadata change transactionally.

        The metadata row is read inside ``BEGIN IMMEDIATE`` so a concurrent
        rotation cannot record a stale ``before`` or lose an audit event. The
        backend write happens before this call; if the transaction fails the
        caller rolls the backend back.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT provider, name, fingerprint, last4, updated_at, updated_by, backend
                    FROM secrets_meta WHERE provider = ? AND name = ?
                    """,
                    (provider, name),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # A pre-v28 database has no table yet; treat it as no row so an
                # explicit initialize() still converges.
                if not _missing_secrets_table(exc):
                    raise
                row = None
            current = _row_to_secret_meta(row) if row is not None else None
            plan = build(current)
            if plan is None:
                connection.commit()
                return current, None
            if plan.record is None:
                connection.execute(
                    "DELETE FROM secrets_meta WHERE provider = ? AND name = ?",
                    (provider, name),
                )
                committed: SecretMetaRecord | None = None
            else:
                connection.execute(
                    """
                    INSERT INTO secrets_meta
                        (provider, name, fingerprint, last4, updated_at, updated_by, backend)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, name) DO UPDATE SET
                        fingerprint = excluded.fingerprint,
                        last4 = excluded.last4,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by,
                        backend = excluded.backend
                    """,
                    (
                        plan.record.provider,
                        plan.record.name,
                        plan.record.fingerprint,
                        plan.record.last4,
                        plan.record.updated_at,
                        plan.record.updated_by,
                        plan.record.backend,
                    ),
                )
                committed = plan.record
            audit_record = (
                self._insert_audit_event(connection, plan.audit) if plan.audit is not None else None
            )
            connection.commit()
            return committed, audit_record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _missing_secrets_table(error: sqlite3.OperationalError) -> bool:
    return "no such table" in str(error).lower()


def _missing_backend_column(error: sqlite3.OperationalError) -> bool:
    return "no such column: backend" in str(error).lower()


def _row_to_secret_meta(row: sqlite3.Row) -> SecretMetaRecord:
    columns = row.keys()
    return SecretMetaRecord(
        provider=str(row["provider"]),
        name=str(row["name"]),
        fingerprint=str(row["fingerprint"]),
        last4=str(row["last4"]),
        updated_at=str(row["updated_at"]),
        updated_by=str(row["updated_by"]),
        backend=str(row["backend"]) if "backend" in columns else "unknown",
    )


__all__ = ["SecretStoreMixin"]
