"""Audit-chain mixin: the append-only ``audit_events`` chain and read accessors.

Owns the ``audit_events`` chain insert, the ``system_state`` entry named by
``AUDIT_HEAD_KEY`` (``audit_head``), which records the last (seq, event_hash)
written so tail truncation stays detectable (ADR-0006, D7), and the read
accessors over the chain. The ``audit_events`` table DDL and its append-only
triggers live in ``sqlite_schema.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from oft.domain.audit import AUDIT_HEAD_KEY, build_audit_entry
from oft.ports.store import AuditEventDraft, AuditEventRecord


class SqliteAuditMixin:
    """Insert and read the audit chain and advance the ``system_state`` head."""

    # Supplied by ``SqliteConnectionMixin`` in every concrete composition.
    _connect: Callable[[], sqlite3.Connection]
    _connect_read_only: Callable[[], sqlite3.Connection | None]

    def _insert_audit_event(
        self, connection: sqlite3.Connection, draft: AuditEventDraft
    ) -> AuditEventRecord:
        previous = connection.execute(
            "SELECT seq, event_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        entry = build_audit_entry(
            previous_seq=None if previous is None else int(previous["seq"]),
            previous_hash=None if previous is None else str(previous["event_hash"]),
            event_type=draft.event_type,
            actor=draft.actor,
            subject=draft.subject,
            payload=draft.payload,
            created_at=draft.created_at,
        )
        record = AuditEventRecord(
            event_id=entry.event_id,
            seq=entry.seq,
            event_type=draft.event_type,
            created_at=draft.created_at,
            previous_hash=entry.previous_hash,
            event_hash=entry.event_hash,
            payload_json=entry.payload_json,
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, seq, event_type, created_at, previous_hash,
                event_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id,
                record.seq,
                record.event_type,
                record.created_at,
                record.previous_hash,
                record.event_hash,
                record.payload_json,
            ),
        )
        # D7: a high-water mark outside the append-only table, written in the
        # same transaction. Deleting the newest rows leaves a gap the chain
        # scan alone cannot see; the head can.
        connection.execute(
            """
            INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (
                AUDIT_HEAD_KEY,
                json.dumps({"seq": record.seq, "event_hash": record.event_hash}, sort_keys=True),
                record.created_at,
            ),
        )
        return record

    def append_audit_event(self, draft: AuditEventDraft) -> AuditEventRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._insert_audit_event(connection, draft)
            connection.commit()
            return record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_audit_events(self, *, limit: int = 100) -> tuple[AuditEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_audit(row) for row in rows)

    def list_audit_events_ascending(
        self, *, limit: int | None = None
    ) -> tuple[AuditEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            if limit is None:
                rows = connection.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY seq ASC LIMIT ?", (limit,)
                ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_audit(row) for row in rows)

    def last_audit_event(self) -> AuditEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return _row_to_audit(row) if row is not None else None

    def audit_store_initialized(self) -> bool:
        """Whether the DB exists and carries the append-only audit table (D7)."""
        connection = self._connect_read_only()
        if connection is None:
            return False
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_events'"
            ).fetchone()
        finally:
            connection.close()
        return row is not None


def _row_to_audit(row: sqlite3.Row) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=row["event_id"],
        seq=row["seq"],
        event_type=row["event_type"],
        created_at=row["created_at"],
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
        payload_json=row["payload_json"],
    )
