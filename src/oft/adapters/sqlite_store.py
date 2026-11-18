"""SQLite transactional store (WAL) composition root.

Composes the connection, audit, LLM-ledger, opportunity-ledger, paper-OMS,
scan, bounded report-read, symbol, backfill, job, and prompt mixins. The schema
DDL and every migration live in ``sqlite_schema.py``. The daemon is the only
writer. Diagnostic commands open the database with ``mode=ro`` so ``oft status``
/ ``oft doctor`` never block or create files.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_backfill_store import BackfillStoreMixin
from oft.adapters.sqlite_config_store import ConfigStoreMixin
from oft.adapters.sqlite_config_store import _row_to_config as _config_row_to_config
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
from oft.config import ConfigError, Settings
from oft.domain.telegram import (
    MAX_TELEGRAM_OUTBOX_ATTEMPTS,
    TELEGRAM_ALLOWED_USER_IDS_KEY,
    TELEGRAM_CHAT_IDS_KEY,
    TELEGRAM_OUTBOX_CLAIM_LEASE_SECONDS,
    TELEGRAM_OUTBOX_MAX_PENDING,
    TELEGRAM_OUTBOX_MAX_PENDING_PER_CHAT,
    TELEGRAM_OUTBOX_OVERFLOW_KEY,
    TELEGRAM_QUEUE_FULL,
    ReceiptState,
)
from oft.domain.time import utc_timestamp
from oft.ports.store import (
    AuditEventDraft,
    AuditEventRecord,
    ConfigRecord,
    StoreUnavailableError,
    TelegramChatStatus,
    TelegramOutboxRecord,
)

__all__ = ["SqliteStore", "database_path", "_period_parts"]

# A ``sqlite3.DatabaseError`` is too broad to mean "corrupt": ``OperationalError``
# covers transient locks, permissions, and disk-full, and ``ProgrammingError`` is
# a bug. Classify by marker so the operator is pointed at the real remedy.
_CORRUPTION_MARKERS = ("file is not a database", "malformed", "encrypted")
_LOCK_MARKERS = ("database is locked", "database is busy", "table is locked")
_OPEN_OR_PERMISSION_MARKERS = (
    "unable to open database file",
    "readonly",
    "read-only",
    "disk is full",
)


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
        try:
            initialize_database(self._settings)
        except sqlite3.IntegrityError:
            # A migration abort names the offending row/table; callers (and
            # operators) must keep that actionable type and message.
            raise
        except sqlite3.DatabaseError as exc:
            detail = str(exc).lower()
            if any(marker in detail for marker in _CORRUPTION_MARKERS):
                # A corrupt/unreadable file is a typed data failure, never a raw
                # runner traceback; the message is bounded and names the next step.
                raise StoreUnavailableError(
                    f"the local database at {self._path} is unreadable; "
                    "move or repair it, then run `oft doctor`"
                ) from exc
            if isinstance(exc, sqlite3.OperationalError):
                if any(marker in detail for marker in _LOCK_MARKERS):
                    # A concurrent writer is not corruption; say so and name the
                    # process the operator must stop.
                    raise StoreUnavailableError(
                        f"the local database at {self._path} is busy: another "
                        "process (for example `oft serve`) is using it; stop that "
                        "process and retry"
                    ) from exc
                if any(marker in detail for marker in _OPEN_OR_PERMISSION_MARKERS):
                    # Opening/writing failed: a configuration problem (exit 5),
                    # not an unreadable database.
                    raise ConfigError(
                        f"cannot open or write the local database at {self._path}; "
                        "check its permissions and free disk space, then run "
                        "`oft doctor`"
                    ) from exc
            # A programming/internal error is a bug, not a data failure: let the
            # caller's INTERNAL_ERROR fallback report it without raw text.
            raise

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

    # -- Telegram allowlists and outbox ----------------------------------

    def save_telegram_allowlist_change(
        self,
        build: Callable[
            [ConfigRecord | None, ConfigRecord | None],
            tuple[ConfigRecord, ConfigRecord, AuditEventDraft] | None,
        ],
    ) -> tuple[ConfigRecord | None, ConfigRecord | None, AuditEventRecord | None]:
        """Read, decide, and write both Telegram allowlist rows atomically."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            chat_row = connection.execute(
                "SELECT key, value_json, updated_at, updated_by FROM config WHERE key = ?",
                (TELEGRAM_CHAT_IDS_KEY,),
            ).fetchone()
            user_row = connection.execute(
                "SELECT key, value_json, updated_at, updated_by FROM config WHERE key = ?",
                (TELEGRAM_ALLOWED_USER_IDS_KEY,),
            ).fetchone()
            chat_current = _row_to_config(chat_row)
            user_current = _row_to_config(user_row)
            outcome = build(chat_current, user_current)
            if outcome is None:
                connection.commit()
                return chat_current, user_current, None
            chat_record, user_record, audit = outcome
            self._upsert_config(connection, chat_record)
            self._upsert_config(connection, user_record)
            record = self._insert_audit_event(connection, audit)
            connection.commit()
            return chat_record, user_record, record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_telegram_settings_change(
        self,
        keys: Sequence[str],
        build: Callable[
            [dict[str, ConfigRecord | None]],
            tuple[Sequence[ConfigRecord], AuditEventDraft] | None,
        ],
    ) -> tuple[dict[str, ConfigRecord | None], AuditEventRecord | None]:
        """Read, decide, and write the managed Telegram settings atomically."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current: dict[str, ConfigRecord | None] = {}
            for key in keys:
                row = connection.execute(
                    "SELECT key, value_json, updated_at, updated_by FROM config WHERE key = ?",
                    (key,),
                ).fetchone()
                current[key] = _config_row_to_config(row) if row is not None else None
            outcome = build(current)
            if outcome is None:
                connection.commit()
                return current, None
            records, audit = outcome
            for record in records:
                self._upsert_config(connection, record)
            audit_record = self._insert_audit_event(connection, audit)
            connection.commit()
            return current, audit_record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_telegram_outbox(self, record: TelegramOutboxRecord) -> bool:
        """Insert one outbox row; a duplicate ``event_key`` is a no-op.

        The duplicate probe runs first, so a replay arriving while the queue is
        full never sets the overflow marker. A genuinely new non-terminal row
        respects the global/per-chat caps; an overflow is rejected (``False``)
        and recorded under :data:`TELEGRAM_OUTBOX_OVERFLOW_KEY` with the stable
        :data:`TELEGRAM_QUEUE_FULL` code. Probe, count, rejection, and insert
        share one write transaction, so concurrent bursts cannot race the bound.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM telegram_outbox WHERE event_key = ?", (record.event_key,)
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                return False
            if record.state in (ReceiptState.QUEUED, ReceiptState.SENDING):
                pending = connection.execute(
                    "SELECT COUNT(*) AS total FROM telegram_outbox "
                    "WHERE state IN ('queued','sending')"
                ).fetchone()
                total = int(pending["total"]) if pending is not None else 0
                chat_row = connection.execute(
                    "SELECT COUNT(*) AS total FROM telegram_outbox "
                    "WHERE state IN ('queued','sending') AND chat_id = ?",
                    (record.chat_id,),
                ).fetchone()
                chat_total = int(chat_row["total"]) if chat_row is not None else 0
                if (
                    total >= TELEGRAM_OUTBOX_MAX_PENDING
                    or chat_total >= TELEGRAM_OUTBOX_MAX_PENDING_PER_CHAT
                ):
                    connection.execute(
                        """
                        INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at
                        """,
                        (
                            TELEGRAM_OUTBOX_OVERFLOW_KEY,
                            TELEGRAM_QUEUE_FULL,
                            record.created_at,
                        ),
                    )
                    connection.commit()
                    return False
            cursor = connection.execute(
                """
                INSERT INTO telegram_outbox (
                    event_key, kind, chat_id, reply_to_message_id, payload_json,
                    priority, state, attempts, next_attempt_at, message_id,
                    last_error_code, created_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                _telegram_outbox_values(record),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                _clear_outbox_overflow_if_drained(connection)
            connection.commit()
            return inserted
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_due_telegram_outbox(
        self, *, now: str, limit: int = 10
    ) -> tuple[TelegramOutboxRecord, ...]:
        """Claim due ``queued`` rows and reclaim expired ``sending`` claims.

        Inside one ``BEGIN IMMEDIATE`` transaction, a stale claim (a row still
        ``sending`` after the lease expired) is requeued as a new attempt, or
        terminally failed when it is already at the attempt cap. A claimed row's
        ``next_attempt_at`` becomes the lease expiry, so a crash during the send
        is recovered on a later tick instead of stranding the reply forever. A
        send that completed but whose terminal write failed is re-sent after the
        lease: a rare duplicate is preferred to a permanently lost reply.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            reaped = connection.execute(
                """
                UPDATE telegram_outbox
                SET attempts = attempts + 1,
                    state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'queued' END,
                    last_error_code = 'TELEGRAM_DELIVERY_FAILED',
                    next_attempt_at = ?
                WHERE state = 'sending' AND next_attempt_at <= ?
                """,
                (MAX_TELEGRAM_OUTBOX_ATTEMPTS, now, now),
            )
            if reaped.rowcount > 0:
                _clear_outbox_overflow_if_drained(connection)
            rows = connection.execute(
                """
                WITH due AS (
                    SELECT event_key, kind, chat_id, reply_to_message_id, payload_json,
                           priority, state, attempts, next_attempt_at, message_id,
                           last_error_code, created_at, sent_at, rowid AS rid
                    FROM telegram_outbox
                    WHERE state = 'queued'
                      AND attempts < ?
                      AND next_attempt_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_outbox AS earlier
                          WHERE earlier.chat_id = telegram_outbox.chat_id
                            AND earlier.state IN ('queued','sending')
                            AND (
                                earlier.created_at < telegram_outbox.created_at
                                OR (
                                    earlier.created_at = telegram_outbox.created_at
                                    AND earlier.rowid < telegram_outbox.rowid
                                )
                            )
                            AND NOT (
                                earlier.state = 'queued'
                                AND earlier.attempts < ?
                                AND earlier.next_attempt_at <= ?
                            )
                      )
                ),
                ranked AS (
                    SELECT due.*,
                           MAX(priority) OVER (PARTITION BY chat_id) AS chat_priority
                    FROM due
                )
                SELECT event_key, kind, chat_id, reply_to_message_id, payload_json,
                       priority, state, attempts, next_attempt_at, message_id,
                       last_error_code, created_at, sent_at
                FROM ranked
                ORDER BY chat_priority DESC, chat_id ASC, created_at ASC, rid ASC
                LIMIT ?
                """,
                (
                    MAX_TELEGRAM_OUTBOX_ATTEMPTS,
                    now,
                    MAX_TELEGRAM_OUTBOX_ATTEMPTS,
                    now,
                    max(1, limit),
                ),
            ).fetchall()
            lease_until = _shift_timestamp(now, TELEGRAM_OUTBOX_CLAIM_LEASE_SECONDS)
            claimed: list[TelegramOutboxRecord] = []
            for row in rows:
                event_key = str(row["event_key"])
                updated = connection.execute(
                    """
                    UPDATE telegram_outbox SET state = 'sending', next_attempt_at = ?
                    WHERE event_key = ? AND state = 'queued'
                    """,
                    (lease_until, event_key),
                )
                if updated.rowcount == 1:
                    claimed.append(
                        replace(
                            _row_to_telegram_outbox(row),
                            state=ReceiptState.SENDING,
                        )
                    )
            connection.commit()
            return tuple(claimed)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_telegram_outbox(self, event_key: str) -> TelegramOutboxRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT event_key, kind, chat_id, reply_to_message_id, payload_json,
                       priority, state, attempts, next_attempt_at, message_id,
                       last_error_code, created_at, sent_at
                FROM telegram_outbox WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return None
            raise
        finally:
            connection.close()
        return _row_to_telegram_outbox(row) if row is not None else None

    def mark_telegram_sent(self, event_key: str, *, message_id: int, sent_at: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE telegram_outbox
                SET state = 'sent', message_id = ?, sent_at = ?, last_error_code = NULL
                WHERE event_key = ? AND state = 'sending'
                """,
                (message_id, sent_at, event_key),
            )
            if cursor.rowcount == 1:
                _clear_outbox_overflow_if_drained(connection)
        return cursor.rowcount == 1

    def mark_telegram_attempt_failed(
        self,
        event_key: str,
        *,
        error_code: str,
        next_attempt_at: str,
        final: bool,
        count_attempt: bool = True,
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE telegram_outbox
                SET attempts = attempts + ?,
                    state = ?,
                    last_error_code = ?,
                    next_attempt_at = ?
                WHERE event_key = ? AND state = 'sending'
                """,
                (
                    1 if count_attempt else 0,
                    "failed" if final else "queued",
                    error_code,
                    next_attempt_at,
                    event_key,
                ),
            )
            if final and cursor.rowcount == 1:
                _clear_outbox_overflow_if_drained(connection)
        return cursor.rowcount == 1

    def release_telegram_claim(self, event_key: str, *, next_attempt_at: str) -> bool:
        """Return an unsent claimed row to ``queued`` without a failure."""
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE telegram_outbox
                SET state = 'queued', next_attempt_at = ?
                WHERE event_key = ? AND state = 'sending'
                """,
                (next_attempt_at, event_key),
            )
        return cursor.rowcount == 1

    def purge_terminal_telegram_outbox(self, *, before: str, limit: int = 1000) -> int:
        """Delete up to ``limit`` old terminal outbox rows.

        Only ``sent``/``failed`` rows created before ``before`` are eligible, so
        purge never removes a queued/sending reply. SQLite builds without
        ``DELETE ... LIMIT`` are supported through a rowid subquery.
        """
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                DELETE FROM telegram_outbox
                WHERE rowid IN (
                    SELECT rowid FROM telegram_outbox
                    WHERE state IN ('sent','failed') AND created_at < ?
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (before, max(1, limit)),
            )
        return cursor.rowcount

    def list_telegram_receipts(self, limit: int = 50) -> tuple[TelegramOutboxRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                """
                SELECT event_key, kind, chat_id, reply_to_message_id, payload_json,
                       priority, state, attempts, next_attempt_at, message_id,
                       last_error_code, created_at, sent_at
                FROM telegram_outbox
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return ()
            raise
        finally:
            connection.close()
        return tuple(_row_to_telegram_outbox(row) for row in rows)

    def count_telegram_receipts(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM telegram_outbox").fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return 0
            raise
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def telegram_receipt_counts(self) -> dict[str, int]:
        counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0}
        connection = self._connect_read_only()
        if connection is None:
            return counts
        try:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS total FROM telegram_outbox GROUP BY state"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return counts
            raise
        finally:
            connection.close()
        for row in rows:
            state = str(row["state"])
            if state in counts:
                counts[state] = int(row["total"])
        return counts

    def telegram_chat_statuses(self, limit: int = 20) -> tuple[TelegramChatStatus, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                """
                SELECT t.chat_id,
                       SUM(CASE WHEN t.state = 'queued' THEN 1 ELSE 0 END) AS queued,
                       SUM(CASE WHEN t.state = 'sending' THEN 1 ELSE 0 END) AS sending,
                       SUM(CASE WHEN t.state = 'sent' THEN 1 ELSE 0 END) AS sent,
                       SUM(CASE WHEN t.state = 'failed' THEN 1 ELSE 0 END) AS failed,
                       (
                           SELECT newest.last_error_code
                           FROM telegram_outbox AS newest
                           WHERE newest.chat_id = t.chat_id AND newest.state = 'failed'
                           ORDER BY newest.created_at DESC, newest.rowid DESC
                           LIMIT 1
                       ) AS last_error_code,
                       MAX(t.created_at) AS newest
                FROM telegram_outbox AS t
                GROUP BY t.chat_id
                ORDER BY newest DESC, t.chat_id ASC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return ()
            raise
        finally:
            connection.close()
        return tuple(
            TelegramChatStatus(
                chat_id=int(row["chat_id"]),
                queued=int(row["queued"] or 0),
                sending=int(row["sending"] or 0),
                sent=int(row["sent"] or 0),
                failed=int(row["failed"] or 0),
                last_error_code=(
                    None if row["last_error_code"] is None else str(row["last_error_code"])
                ),
            )
            for row in rows
        )

    def last_telegram_sent_at(self) -> str | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT MAX(sent_at) AS newest FROM telegram_outbox WHERE state = 'sent'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_telegram_outbox(exc):
                return None
            raise
        finally:
            connection.close()
        if row is None or row["newest"] is None:
            return None
        return str(row["newest"])


_TELEGRAM_OUTBOX_TABLE = "telegram_outbox"


def _clear_outbox_overflow_if_drained(connection: sqlite3.Connection) -> None:
    """Drop the overflow marker once the pending queue is under both caps."""
    marker = connection.execute(
        "SELECT 1 FROM system_state WHERE key = ?", (TELEGRAM_OUTBOX_OVERFLOW_KEY,)
    ).fetchone()
    if marker is None:
        return
    row = connection.execute(
        "SELECT COALESCE(SUM(pending), 0) AS total, COALESCE(MAX(pending), 0) AS highest "
        "FROM (SELECT COUNT(*) AS pending FROM telegram_outbox "
        "WHERE state IN ('queued','sending') GROUP BY chat_id)"
    ).fetchone()
    if row is None:
        return
    total = int(row["total"])
    highest = int(row["highest"])
    if total >= TELEGRAM_OUTBOX_MAX_PENDING or highest >= TELEGRAM_OUTBOX_MAX_PENDING_PER_CHAT:
        return
    connection.execute("DELETE FROM system_state WHERE key = ?", (TELEGRAM_OUTBOX_OVERFLOW_KEY,))


def _shift_timestamp(value: str, seconds: int) -> str:
    """Shift one canonical ISO-8601 UTC timestamp by ``seconds``."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_timestamp(parsed + timedelta(seconds=seconds))


def _missing_telegram_outbox(exc: sqlite3.OperationalError) -> bool:
    return _TELEGRAM_OUTBOX_TABLE in str(exc).lower() and "no such table" in str(exc).lower()


def _row_to_config(row: sqlite3.Row | None) -> ConfigRecord | None:
    return None if row is None else _config_row_to_config(row)


def _telegram_outbox_values(record: TelegramOutboxRecord) -> tuple[object, ...]:
    return (
        record.event_key,
        record.kind,
        record.chat_id,
        record.reply_to_message_id,
        record.payload_json,
        record.priority,
        record.state,
        record.attempts,
        record.next_attempt_at,
        record.message_id,
        record.last_error_code,
        record.created_at,
        record.sent_at,
    )


def _row_to_telegram_outbox(row: sqlite3.Row) -> TelegramOutboxRecord:
    return TelegramOutboxRecord(
        event_key=str(row["event_key"]),
        kind=str(row["kind"]),
        chat_id=int(row["chat_id"]),
        reply_to_message_id=(
            None if row["reply_to_message_id"] is None else int(row["reply_to_message_id"])
        ),
        payload_json=str(row["payload_json"]),
        priority=int(row["priority"]),
        state=ReceiptState(str(row["state"])),
        attempts=int(row["attempts"]),
        next_attempt_at=str(row["next_attempt_at"]),
        message_id=None if row["message_id"] is None else int(row["message_id"]),
        last_error_code=(None if row["last_error_code"] is None else str(row["last_error_code"])),
        created_at=str(row["created_at"]),
        sent_at=None if row["sent_at"] is None else str(row["sent_at"]),
    )
