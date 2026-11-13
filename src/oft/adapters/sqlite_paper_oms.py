"""Paper OMS mixin: orders, fills, positions, and paper accounts.

Covers the ``orders``, ``fills``, ``positions``, and ``paper_accounts`` tables
plus their row mappers.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import ROUND_DOWN, Decimal

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.ports.store import (
    AuditEventDraft,
    FillRecord,
    OmsCommit,
    OmsConflictError,
    OrderRecord,
    PaperAccountRecord,
    PositionRecord,
)


def _oms_conflict_kind(message: str) -> str:
    """Classify an ``orders`` unique violation for the gateway's reconciliation."""
    if "orders.trigger_id" in message or "orders.environment" in message:
        return "trigger_entry"
    if "orders.idempotency_key" in message:
        return "idempotency_key"
    if "orders.client_order_id" in message:
        return "client_order_id"
    return "unknown"


class PaperOmsStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write paper-OMS rows: orders, fills, positions, and accounts."""

    # -- Paper OMS -------------------------------------------------------

    def save_order_fill_position(
        self,
        *,
        order: OrderRecord,
        fill: FillRecord,
        position: PositionRecord,
        account: PaperAccountRecord,
        balance_delta: str = "0",
        audit: AuditEventDraft | None = None,
    ) -> OmsCommit:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audit_record = (
                self._insert_audit_event(connection, audit) if audit is not None else None
            )
            # Read the account inside the write transaction: an absolute balance
            # computed by a caller would lose one of two concurrent debits.
            existing = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (account.account_id,)
            ).fetchone()
            if existing is not None:
                base = Decimal(str(existing["balance"]))
                initial_balance = str(existing["initial_balance"])
                environment = str(existing["environment"])
                currency = str(existing["currency"])
            else:
                base = Decimal(account.initial_balance)
                initial_balance = account.initial_balance
                environment = account.environment
                currency = account.currency
            balance = (base + Decimal(balance_delta)).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, client_order_id, environment, symbol, status,
                    risk_verdict_id, idempotency_key, created_at, updated_at,
                    payload_json, trigger_id, decision_id, purpose
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.client_order_id,
                    order.environment,
                    order.symbol,
                    order.status,
                    order.risk_verdict_id,
                    order.idempotency_key,
                    order.created_at,
                    order.updated_at,
                    order.payload_json,
                    order.trigger_id,
                    order.decision_id,
                    order.purpose,
                ),
            )
            connection.execute(
                """
                INSERT INTO fills (
                    fill_id, order_id, environment, symbol, filled_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.environment,
                    fill.symbol,
                    fill.filled_at,
                    fill.payload_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO positions (
                    position_id, account_id, environment, symbol, state,
                    opened_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.position_id,
                    position.account_id,
                    position.environment,
                    position.symbol,
                    position.state,
                    position.opened_at,
                    position.payload_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, environment, currency, initial_balance, balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (
                    account.account_id,
                    environment,
                    currency,
                    initial_balance,
                    str(balance),
                    account.updated_at,
                ),
            )
            connection.commit()
            committed_account = PaperAccountRecord(
                account_id=account.account_id,
                environment=environment,
                currency=currency,
                initial_balance=initial_balance,
                balance=str(balance),
                updated_at=account.updated_at,
            )
            return OmsCommit(account=committed_account, audit=audit_record)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OmsConflictError(_oms_conflict_kind(str(exc))) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_order(self, order_id: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def get_order_by_idempotency(self, idempotency_key: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def get_order_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def get_entry_order_for_trigger(self, trigger_id: str) -> OrderRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM orders WHERE trigger_id = ? AND purpose = 'ENTRY' "
                "AND environment = 'PAPER' "
                "ORDER BY created_at ASC, order_id ASC LIMIT 1",
                (trigger_id,),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_order(row) if row is not None else None

    def list_orders(self, *, limit: int = 100, offset: int = 0) -> tuple[OrderRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_order(row) for row in rows)

    def count_orders(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM orders").fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def list_fills(self, *, limit: int = 100, offset: int = 0) -> tuple[FillRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_fill(row) for row in rows)

    def count_fills(self) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM fills").fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def get_fill_by_order_id(self, order_id: str) -> FillRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM fills WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_fill(row) if row is not None else None

    def list_positions(
        self, *, state: str | None = None, limit: int | None = 100, offset: int = 0
    ) -> tuple[PositionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM positions{where} ORDER BY opened_at DESC"
        # An equity total must be exact: ``limit=None`` reads every open row
        # instead of silently truncating at the default page size (DB-5).
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        try:
            rows = connection.execute(query, tuple(params)).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_position(row) for row in rows)

    def count_positions(self, *, state: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if state is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM positions").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM positions WHERE state = ?", (state,)
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def get_position(self, position_id: str) -> PositionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_position(row) if row is not None else None

    def get_paper_account(self, account_id: str) -> PaperAccountRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_paper_account(row) if row is not None else None

    def save_paper_account(self, record: PaperAccountRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, environment, currency, initial_balance, balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (
                    record.account_id,
                    record.environment,
                    record.currency,
                    record.initial_balance,
                    record.balance,
                    record.updated_at,
                ),
            )

    def seed_paper_account(self, record: PaperAccountRecord) -> PaperAccountRecord:
        """Insert the seed account once and return the row actually stored."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, environment, currency, initial_balance, balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (
                    record.account_id,
                    record.environment,
                    record.currency,
                    record.initial_balance,
                    record.balance,
                    record.updated_at,
                ),
            )
        stored = self.get_paper_account(record.account_id)
        return stored if stored is not None else record


def _row_to_order(row: sqlite3.Row) -> OrderRecord:
    keys = row.keys()
    return OrderRecord(
        order_id=row["order_id"],
        client_order_id=row["client_order_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        status=row["status"],
        risk_verdict_id=row["risk_verdict_id"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload_json=row["payload_json"],
        trigger_id=row["trigger_id"] if "trigger_id" in keys else None,
        decision_id=row["decision_id"] if "decision_id" in keys else None,
        purpose=row["purpose"] if "purpose" in keys else "ENTRY",
    )


def _row_to_fill(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        fill_id=row["fill_id"],
        order_id=row["order_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        filled_at=row["filled_at"],
        payload_json=row["payload_json"],
    )


def _row_to_position(row: sqlite3.Row) -> PositionRecord:
    return PositionRecord(
        position_id=row["position_id"],
        account_id=row["account_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        state=row["state"],
        opened_at=row["opened_at"],
        payload_json=row["payload_json"],
    )


def _row_to_paper_account(row: sqlite3.Row) -> PaperAccountRecord:
    return PaperAccountRecord(
        account_id=row["account_id"],
        environment=row["environment"],
        currency=row["currency"],
        initial_balance=row["initial_balance"],
        balance=row["balance"],
        updated_at=row["updated_at"],
    )
