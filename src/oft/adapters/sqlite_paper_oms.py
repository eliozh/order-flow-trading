"""Paper OMS mixin: orders, fills, positions, trade logs, and funding accruals.

Covers the ``orders``, ``fills``, ``positions``, ``trade_logs``,
``position_events``, ``funding_accruals``, and ``paper_accounts`` tables plus
their row mappers and the atomic open-position update used by adjustments and
funding.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import ROUND_DOWN, Decimal

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.adapters.sqlite_scan_store import _row_to_report
from oft.ports.store import (
    AuditEventDraft,
    CloseCommitResult,
    ClosedPositionRecord,
    FillRecord,
    FundingAccrualCommit,
    FundingAccrualRecord,
    OmsCommit,
    OmsConflictError,
    OrderRecord,
    PaperAccountRecord,
    PositionAdjustmentRecord,
    PositionEventRecord,
    PositionRecord,
    ReportRecord,
    TradeLogRecord,
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


def _close_conflict_kind(message: str) -> str:
    """Classify a constraint failure inside a close commit."""
    kind = _oms_conflict_kind(message)
    if kind != "unknown":
        return kind
    if "trade_logs.position_id" in message:
        return "trade_log"
    return "close"


def _account_base(
    connection: sqlite3.Connection,
    record: ClosedPositionRecord | PositionAdjustmentRecord | FundingAccrualCommit,
) -> tuple[Decimal, str, str, str]:
    """Read the live account inside the transaction, or use the seed defaults."""
    existing = connection.execute(
        "SELECT * FROM paper_accounts WHERE account_id = ?",
        (record.account.account_id,),
    ).fetchone()
    if existing is not None:
        return (
            Decimal(str(existing["balance"])),
            str(existing["initial_balance"]),
            str(existing["environment"]),
            str(existing["currency"]),
        )
    return (
        Decimal(record.account.initial_balance),
        record.account.initial_balance,
        record.account.environment,
        record.account.currency,
    )


def _insert_close_order(connection: sqlite3.Connection, order: OrderRecord) -> None:
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


def _insert_close_fill(connection: sqlite3.Connection, fill: FillRecord) -> None:
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


def _upsert_close_account(
    connection: sqlite3.Connection,
    record: ClosedPositionRecord | PositionAdjustmentRecord | FundingAccrualCommit,
    *,
    balance: Decimal,
    initial_balance: str,
    environment: str,
    currency: str,
) -> None:
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
            record.account.account_id,
            environment,
            currency,
            initial_balance,
            str(balance),
            record.account.updated_at,
        ),
    )


def _mark_position_closed(connection: sqlite3.Connection, record: ClosedPositionRecord) -> None:
    """Flip the one OPEN row to CLOSED; a zero-row update aborts the unit.

    When the record carries ``expected_payload_json`` the update is optimistic:
    it only matches the exact payload the caller read. A concurrent funding
    accrual, partial close, or add that already replaced the row makes this a
    zero-row update, so the whole close rolls back instead of silently erasing
    the other write from the surviving payload.
    """
    if record.position.expected_payload_json is None:
        updated = connection.execute(
            "UPDATE positions SET state = 'CLOSED', payload_json = ? "
            "WHERE position_id = ? AND state = 'OPEN'",
            (record.position.payload_json, record.position.position_id),
        )
        if updated.rowcount != 1:
            raise OmsConflictError("position_state")
        return
    updated = connection.execute(
        "UPDATE positions SET state = 'CLOSED', payload_json = ? "
        "WHERE position_id = ? AND state = 'OPEN' AND payload_json = ?",
        (
            record.position.payload_json,
            record.position.position_id,
            record.position.expected_payload_json,
        ),
    )
    if updated.rowcount != 1:
        raise OmsConflictError("position_version")


def _update_open_position(connection: sqlite3.Connection, position: PositionRecord) -> None:
    """Replace the one OPEN row's payload; a zero-row update aborts the unit.

    When the record carries ``expected_payload_json`` the update is
    optimistic: it only matches the payload the caller read. A concurrent
    adjustment or funding accrual that already replaced the row makes this a
    zero-row update, so the caller can re-read and retry instead of silently
    losing the other write.
    """
    if position.expected_payload_json is None:
        updated = connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ? AND state = 'OPEN'",
            (position.payload_json, position.position_id),
        )
    else:
        updated = connection.execute(
            "UPDATE positions SET payload_json = ? "
            "WHERE position_id = ? AND state = 'OPEN' AND payload_json = ?",
            (position.payload_json, position.position_id, position.expected_payload_json),
        )
    if updated.rowcount != 1:
        raise OmsConflictError(
            "position_state" if position.expected_payload_json is None else "position_version"
        )


def _insert_funding_accrual(connection: sqlite3.Connection, accrual: FundingAccrualRecord) -> bool:
    """Insert the accrual once; ``False`` when ``(position, settlement)`` exists."""
    cursor = connection.execute(
        """
        INSERT INTO funding_accruals (
            accrual_id, position_id, settlement_time, rate, mark_price,
            amount_usd, created_at, side, size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(position_id, settlement_time) DO NOTHING
        """,
        (
            accrual.accrual_id,
            accrual.position_id,
            accrual.settlement_time,
            accrual.rate,
            accrual.mark_price,
            accrual.amount_usd,
            accrual.created_at,
            accrual.side,
            accrual.size,
        ),
    )
    return cursor.rowcount == 1


def _funding_total_advanced(record: FundingAccrualCommit) -> None:
    """Fail closed unless the position's funding advanced by the accrual amount.

    The amount is the only cash movement for a funding settlement, so the
    updated payload's ``funding_usd`` must equal the prior payload's value plus
    ``accrual.amount_usd``. A mismatch means the caller built the commit from
    the wrong payload; committing it would desynchronise cash and the position.
    """
    expected = record.position.expected_payload_json
    if expected is None:
        return
    try:
        prior = json.loads(expected)
        updated = json.loads(record.position.payload_json)
        prior_funding = Decimal(str(prior.get("funding_usd") or "0"))
        new_funding = Decimal(str(updated.get("funding_usd") or "0"))
        amount = Decimal(record.accrual.amount_usd)
    except (json.JSONDecodeError, TypeError, ValueError, ArithmeticError) as exc:
        raise OmsConflictError("funding_total") from exc
    if new_funding != prior_funding + amount:
        raise OmsConflictError("funding_total")


def _insert_position_event(connection: sqlite3.Connection, event: PositionEventRecord) -> None:
    """Append the event at the next contiguous sequence for the position."""
    if event.seq is None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM position_events WHERE position_id = ?",
            (event.position_id,),
        ).fetchone()
        seq = int(sequence["next"]) if sequence is not None else 1
    else:
        seq = event.seq
    connection.execute(
        """
        INSERT INTO position_events (
            position_id, seq, at, action, order_id, note
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.position_id,
            seq,
            event.at,
            event.action,
            event.order_id,
            event.note,
        ),
    )


def _insert_trade_log(connection: sqlite3.Connection, trade_log: TradeLogRecord) -> None:
    connection.execute(
        """
        INSERT INTO trade_logs (
            trade_id, position_id, account_id, environment, symbol, side,
            opened_at, closed_at, net_pnl_usd, idempotency_key,
            payload_json, order_id, fill_id, risk_verdict_id, decision_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_log.trade_id,
            trade_log.position_id,
            trade_log.account_id,
            trade_log.environment,
            trade_log.symbol,
            trade_log.side,
            trade_log.opened_at,
            trade_log.closed_at,
            trade_log.net_pnl_usd,
            trade_log.idempotency_key,
            trade_log.payload_json,
            trade_log.order_id,
            trade_log.fill_id,
            trade_log.risk_verdict_id,
            trade_log.decision_id,
        ),
    )


class PaperOmsStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write paper-OMS rows: orders, fills, positions, events, and funding."""

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
            # The account row is written before the position in the same
            # transaction: ``positions.account_id`` is a foreign key to
            # ``paper_accounts`` (schema v21), so the parent must exist first.
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

    def save_close(self, record: ClosedPositionRecord) -> CloseCommitResult:
        """Commit a full close as one unit of work (no partial transaction).

        The existing position row is updated in place; a zero-row update means
        the position is no longer OPEN and aborts the whole commit. The account
        balance is read and written inside the same transaction so concurrent
        fills cannot overwrite each other's delta.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            audits = tuple(self._insert_audit_event(connection, draft) for draft in record.audits)
            base, initial_balance, environment, currency = _account_base(connection, record)
            balance = (base + Decimal(record.balance_delta)).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            _insert_close_order(connection, record.order)
            _insert_close_fill(connection, record.fill)
            _upsert_close_account(
                connection,
                record,
                balance=balance,
                initial_balance=initial_balance,
                environment=environment,
                currency=currency,
            )
            _mark_position_closed(connection, record)
            _insert_position_event(connection, record.event)
            _insert_trade_log(connection, record.trade_log)
            connection.commit()
            committed = PaperAccountRecord(
                account_id=record.account.account_id,
                environment=environment,
                currency=currency,
                initial_balance=initial_balance,
                balance=str(balance),
                updated_at=record.account.updated_at,
            )
            return CloseCommitResult(
                account=committed,
                trade_log=record.trade_log,
                audits=audits,
            )
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OmsConflictError(_close_conflict_kind(str(exc))) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _commit_open_update(
        self,
        record: PositionAdjustmentRecord | FundingAccrualCommit,
        *,
        balance_delta: Decimal,
        precheck: Callable[[sqlite3.Connection], bool] | None = None,
    ) -> OmsCommit | None:
        """Commit one update to the one OPEN position as an atomic unit.

        Shared by a partial close/add and a funding accrual: both may insert an
        order/fill (adjustments), move the account balance by a derived delta,
        replace the OPEN payload optimistically, and append one event. Funding
        passes a precheck that inserts its unique accrual row first, so a replay
        returns ``None`` without touching the account or position.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if precheck is not None and not precheck(connection):
                connection.rollback()
                return None
            audits = tuple(self._insert_audit_event(connection, draft) for draft in record.audits)
            base, initial_balance, environment, currency = _account_base(connection, record)
            balance = (base + balance_delta).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if isinstance(record, PositionAdjustmentRecord):
                _insert_close_order(connection, record.order)
                _insert_close_fill(connection, record.fill)
            _upsert_close_account(
                connection,
                record,
                balance=balance,
                initial_balance=initial_balance,
                environment=environment,
                currency=currency,
            )
            _update_open_position(connection, record.position)
            _insert_position_event(connection, record.event)
            connection.commit()
            committed = PaperAccountRecord(
                account_id=record.account.account_id,
                environment=environment,
                currency=currency,
                initial_balance=initial_balance,
                balance=str(balance),
                updated_at=record.account.updated_at,
            )
            return OmsCommit(account=committed, audit=audits[-1] if audits else None)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OmsConflictError(_close_conflict_kind(str(exc))) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_position_adjustment(self, record: PositionAdjustmentRecord) -> OmsCommit:
        """Commit a partial close or add as one unit; no trade log is written."""
        result = self._commit_open_update(record, balance_delta=Decimal(record.balance_delta))
        # An adjustment passes no precheck, so the shared commit never returns
        # the "duplicate/no-op" None that a funding accrual can.
        assert result is not None, "an adjustment commit must write the position"
        return result

    def save_funding_accrual(self, record: FundingAccrualCommit) -> bool:
        """Commit one funding settlement once; a replay applies nothing.

        The cash delta and the position's funding total are both derived from
        ``record.accrual.amount_usd``; the total is verified before commit. A
        duplicate ``(position, settlement)`` returns ``False`` before any
        account or position write.
        """

        def precheck(connection: sqlite3.Connection) -> bool:
            if not _insert_funding_accrual(connection, record.accrual):
                return False
            _funding_total_advanced(record)
            return True

        result = self._commit_open_update(
            record,
            balance_delta=Decimal(record.accrual.amount_usd),
            precheck=precheck,
        )
        return result is not None

    def list_funding_accruals(self, position_id: str) -> tuple[FundingAccrualRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT accrual_id, position_id, settlement_time, rate, mark_price, "
                "amount_usd, created_at, side, size FROM funding_accruals WHERE position_id = ? "
                "ORDER BY settlement_time ASC, accrual_id ASC",
                (position_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_funding_accrual(row) for row in rows)

    _REPORT_COLUMNS = (
        "report_id, scan_id, symbol, timeframe, generated_at, metrics_manifest_json, "
        "llm_decision_id, narrative_ref, data_quality, snapshot_json, metrics_json, "
        "llm_json, window_policy_json"
    )

    def list_reports_for_symbol(
        self, symbol: str, *, since: str | None = None, limit: int | None = None
    ) -> tuple[ReportRecord, ...]:
        """Recorded reports for one symbol, oldest generated first.

        ``since`` bounds the read to the newest reports generated at/after a
        position's opening plus the single newest report generated before it.
        The pre-entry report is retained because it is the frozen source the
        position was opened from, while a settlement inside the holding window
        can only appear in a report generated after the position opened.
        ``limit`` caps the post-entry rows, so a long history cannot turn one
        monitor tick into an unbounded snapshot scan. The
        ``reports_symbol_generated`` index backs both queries.
        """
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            if since is None:
                query = (
                    f"SELECT {self._REPORT_COLUMNS} FROM reports WHERE symbol = ? "
                    "ORDER BY generated_at DESC, report_id DESC"
                )
                params: list[object] = [symbol]
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(int(limit))
                rows = list(connection.execute(query, tuple(params)).fetchall())
            else:
                query = (
                    f"SELECT {self._REPORT_COLUMNS} FROM reports "
                    "WHERE symbol = ? AND generated_at >= ? "
                    "ORDER BY generated_at DESC, report_id DESC"
                )
                params = [symbol, since]
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(int(limit))
                rows = list(connection.execute(query, tuple(params)).fetchall())
                pre_entry = connection.execute(
                    f"SELECT {self._REPORT_COLUMNS} FROM reports "
                    "WHERE symbol = ? AND generated_at < ? "
                    "ORDER BY generated_at DESC, report_id DESC LIMIT 1",
                    (symbol, since),
                ).fetchone()
                if pre_entry is not None:
                    rows.append(pre_entry)
        finally:
            connection.close()
        records = [_row_to_report(row) for row in rows]
        records.sort(key=lambda record: (record.generated_at, record.report_id))
        return tuple(records)

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

    def update_open_position_payload(
        self,
        position_id: str,
        *,
        payload_json: str,
        expected_payload_json: str | None = None,
    ) -> bool:
        """Update only an OPEN position row; a CLOSED row is left untouched.

        With ``expected_payload_json`` the update is optimistic: it only wins
        when the row still carries the payload the caller read. A concurrent
        funding accrual or adjustment that replaced the row makes it a
        zero-row update, so the caller can re-read instead of losing it.
        """
        with closing(self._connect()) as connection, connection:
            if expected_payload_json is None:
                updated = connection.execute(
                    "UPDATE positions SET payload_json = ? "
                    "WHERE position_id = ? AND state = 'OPEN'",
                    (payload_json, position_id),
                )
            else:
                updated = connection.execute(
                    "UPDATE positions SET payload_json = ? "
                    "WHERE position_id = ? AND state = 'OPEN' AND payload_json = ?",
                    (payload_json, position_id, expected_payload_json),
                )
            return updated.rowcount == 1

    def get_trade_log(self, trade_id: str) -> TradeLogRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trade_logs WHERE trade_id = ?", (trade_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trade_log(row) if row is not None else None

    def get_trade_log_for_position(self, position_id: str) -> TradeLogRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trade_logs WHERE position_id = ?", (position_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trade_log(row) if row is not None else None

    def list_trade_logs(
        self, *, symbol: str | None = None, limit: int | None = 100, offset: int = 0
    ) -> tuple[TradeLogRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        params: list[object] = []
        where = ""
        if symbol is not None:
            where = " WHERE symbol = ?"
            params.append(symbol)
        query = f"SELECT * FROM trade_logs{where} ORDER BY closed_at DESC, trade_id ASC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        try:
            rows = connection.execute(query, tuple(params)).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_trade_log(row) for row in rows)

    def count_trade_logs(self, *, symbol: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if symbol is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM trade_logs").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM trade_logs WHERE symbol = ?", (symbol,)
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def list_position_events(self, position_id: str) -> tuple[PositionEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM position_events WHERE position_id = ? ORDER BY seq ASC",
                (position_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_position_event(row) for row in rows)

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


def _row_to_trade_log(row: sqlite3.Row) -> TradeLogRecord:
    return TradeLogRecord(
        trade_id=row["trade_id"],
        position_id=row["position_id"],
        account_id=row["account_id"],
        environment=row["environment"],
        symbol=row["symbol"],
        side=row["side"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        net_pnl_usd=row["net_pnl_usd"],
        idempotency_key=row["idempotency_key"],
        payload_json=row["payload_json"],
        order_id=row["order_id"],
        fill_id=row["fill_id"],
        risk_verdict_id=row["risk_verdict_id"],
        decision_id=row["decision_id"],
    )


def _row_to_position_event(row: sqlite3.Row) -> PositionEventRecord:
    return PositionEventRecord(
        position_id=row["position_id"],
        seq=row["seq"],
        at=row["at"],
        action=row["action"],
        order_id=row["order_id"],
        note=row["note"],
    )


def _row_to_funding_accrual(row: sqlite3.Row) -> FundingAccrualRecord:
    return FundingAccrualRecord(
        accrual_id=row["accrual_id"],
        position_id=row["position_id"],
        settlement_time=row["settlement_time"],
        rate=row["rate"],
        mark_price=row["mark_price"],
        amount_usd=row["amount_usd"],
        created_at=row["created_at"],
        side=row["side"],
        size=row["size"],
    )
