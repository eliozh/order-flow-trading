"""SQLite transactional store (WAL) composition root.

Composes the connection, audit, LLM-ledger, opportunity-ledger, paper-OMS,
scan, bounded report-read, symbol, backfill, job, and prompt mixins. The schema
DDL and every migration live in ``sqlite_schema.py``. The daemon is the only
writer. Diagnostic commands open the database with ``mode=ro`` so ``oft status``
/ ``oft doctor`` never block or create files.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_backfill_store import BackfillStoreMixin
from oft.adapters.sqlite_config_store import ConfigStoreMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.adapters.sqlite_job_store import JobStoreMixin
from oft.adapters.sqlite_llm_ledger import LLMStoreMixin
from oft.adapters.sqlite_opportunity_ledger import OpportunityStoreMixin
from oft.adapters.sqlite_paper_oms import PaperOmsStoreMixin
from oft.adapters.sqlite_prompt_store import PromptStoreMixin
from oft.adapters.sqlite_report_reads import ReportReadsMixin
from oft.adapters.sqlite_scan_store import ScanStoreMixin
from oft.adapters.sqlite_schema import _period_parts as _period_parts
from oft.adapters.sqlite_schema import database_path, initialize_database
from oft.adapters.sqlite_secrets_store import SecretStoreMixin
from oft.adapters.sqlite_symbol_store import SymbolStoreMixin
from oft.config import Settings
from oft.ports.store import AuditEventDraft, AuditEventRecord

__all__ = ["SqliteStore", "database_path", "_period_parts"]


class SqliteStore(
    LLMStoreMixin,
    OpportunityStoreMixin,
    PaperOmsStoreMixin,
    ScanStoreMixin,
    ReportReadsMixin,
    SymbolStoreMixin,
    BackfillStoreMixin,
    JobStoreMixin,
    PromptStoreMixin,
    SecretStoreMixin,
    ConfigStoreMixin,
    SqliteConnectionMixin,
    SqliteAuditMixin,
):
    """Concrete :class:`oft.ports.store.StorePort` implementation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = database_path(settings)

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        initialize_database(self._settings)

    def integrity_check(self) -> bool:
        connection = self._connect_read_only()
        if connection is None:
            return False
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()
        return bool(row is not None and row[0] == "ok")

    def get_system_state(self, key: str) -> str | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        return str(row["value"]) if row is not None else None

    def get_system_state_updated_at(self, key: str) -> str | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT updated_at FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        return str(row["updated_at"]) if row is not None else None

    def set_system_state(self, key: str, value: str, *, updated_at: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at),
            )

    def save_risk_caps(
        self, *, value: str, updated_at: str, audit: AuditEventDraft
    ) -> AuditEventRecord:
        """Write the caps and their audit event in one unit of work (D4)."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                ("risk_caps", value, updated_at),
            )
            record = self._insert_audit_event(connection, audit)
            connection.commit()
            return record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_risk_caps_change(
        self,
        build: Callable[[str | None], tuple[str, AuditEventDraft] | None],
    ) -> tuple[str | None, AuditEventRecord | None]:
        """Read, decide, and write the risk caps inside one transaction.

        The current ``system_state.risk_caps`` value is read *inside*
        ``BEGIN IMMEDIATE`` so two processes cannot interleave between the
        compare and the upsert. ``build`` is pure: it returns either the complete
        caps document plus its audit draft, or ``None`` (a no-op). A validation
        error raised by ``build`` rolls the whole unit back, so invalid values
        write neither a row nor an audit.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM system_state WHERE key = ?", ("risk_caps",)
            ).fetchone()
            current = None if row is None else str(row["value"])
            outcome = build(current)
            if outcome is None:
                connection.commit()
                return current, None
            value, audit = outcome
            connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                ("risk_caps", value, audit.created_at),
            )
            record = self._insert_audit_event(connection, audit)
            connection.commit()
            return value, record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
