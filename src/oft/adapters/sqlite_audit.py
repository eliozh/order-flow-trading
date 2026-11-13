"""Audit-chain mixin: the append-only ``audit_events`` chain insert and head.

Owns the ``audit_events`` chain insert and the ``system_state`` entry named by
``AUDIT_HEAD_KEY`` (``audit_head``), which records the last (seq, event_hash)
written so tail truncation stays detectable (ADR-0006, D7). The
``audit_events`` table DDL and its append-only triggers live in
``sqlite_store.py``.
"""

from __future__ import annotations

import json
import sqlite3

from oft.domain.audit import AUDIT_HEAD_KEY, build_audit_entry
from oft.ports.store import AuditEventDraft, AuditEventRecord


class SqliteAuditMixin:
    """Insert audit-chain events and advance the ``system_state`` head."""

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
