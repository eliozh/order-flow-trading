"""Persisted runtime settings (``config`` table) with atomic audit writes.

The settings service is the only writer. A save upserts exactly one row and
appends exactly one audit event inside one ``BEGIN IMMEDIATE`` transaction, so a
recorded change can never exist without its audit trail (and vice versa).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing

from oft.ports.store import AuditEventDraft, AuditEventRecord, ConfigRecord


class ConfigStoreMixin:
    """Read and write ``config`` rows; the schema DDL lives in ``sqlite_schema``."""

    # Supplied by ``SqliteConnectionMixin``/``SqliteAuditMixin`` in the concrete
    # composition.
    _connect: Callable[[], sqlite3.Connection]
    _connect_read_only: Callable[[], sqlite3.Connection | None]
    _insert_audit_event: Callable[[sqlite3.Connection, AuditEventDraft], AuditEventRecord]

    def get_config(self, key: str) -> ConfigRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT key, value_json, updated_at, updated_by FROM config WHERE key = ?",
                (key,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_config_table(exc):
                # A pre-v26 data directory has no ``config`` table yet; a
                # read-only diagnostic treats it as "no persisted row".
                return None
            raise
        finally:
            connection.close()
        return _row_to_config(row) if row is not None else None

    def list_config(self) -> tuple[ConfigRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT key, value_json, updated_at, updated_by FROM config ORDER BY key ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_config_table(exc):
                return ()
            raise
        finally:
            connection.close()
        return tuple(_row_to_config(row) for row in rows)

    def set_config(self, record: ConfigRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._upsert_config(connection, record)

    def save_config_with_audit(
        self, record: ConfigRecord, audit: AuditEventDraft
    ) -> AuditEventRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_config(connection, record)
            audit_record = self._insert_audit_event(connection, audit)
            connection.commit()
            return audit_record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _upsert_config(connection: sqlite3.Connection, record: ConfigRecord) -> None:
        connection.execute(
            """
            INSERT INTO config (key, value_json, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (record.key, record.value_json, record.updated_at, record.updated_by),
        )


def _missing_config_table(error: sqlite3.OperationalError) -> bool:
    """True for the pre-v26 "no such table: config" read-only case."""
    return "no such table" in str(error).lower()


def _row_to_config(row: sqlite3.Row) -> ConfigRecord:
    return ConfigRecord(
        key=str(row["key"]),
        value_json=str(row["value_json"]),
        updated_at=str(row["updated_at"]),
        updated_by=str(row["updated_by"]),
    )


__all__ = ["ConfigStoreMixin"]
