"""Trading ledger mixin: opportunities, triggers, risk, and the paper OMS.

Covers the ``opportunities``, ``opportunity_events``, ``opportunity_conditions``,
``trigger_events``, ``risk_verdicts``, ``orders``, ``fills``, ``positions``, and
``paper_accounts`` tables and their row mappers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Literal

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.domain.conditions import normalize_timestamp
from oft.domain.opportunities import _ALLOWED_TRANSITIONS, OpportunityState
from oft.ports.store import (
    AuditEventDraft,
    ConditionRecord,
    FillRecord,
    OmsCommit,
    OmsConflictError,
    OpportunityEventRecord,
    OpportunityRecord,
    OrderRecord,
    PaperAccountRecord,
    PositionRecord,
    RiskVerdictRecord,
    TriggerEventRecord,
)

_LOGGER = logging.getLogger(__name__)


def _oms_conflict_kind(message: str) -> str:
    """Classify an ``orders`` unique violation for the gateway's reconciliation."""
    if "orders.trigger_id" in message or "orders.environment" in message:
        return "trigger_entry"
    if "orders.idempotency_key" in message:
        return "idempotency_key"
    if "orders.client_order_id" in message:
        return "client_order_id"
    return "unknown"


class TradingStoreMixin(SqliteAuditMixin):
    """Read and write trading-ledger state and the paper OMS rows."""

    if TYPE_CHECKING:

        def _connect(self) -> sqlite3.Connection: ...
        def _connect_read_only(self) -> sqlite3.Connection | None: ...

    # -- Opportunity ledger ----------------------------------------------

    def save_opportunity(
        self,
        record: OpportunityRecord,
        event: OpportunityEventRecord | None = None,
        conditions: Sequence[ConditionRecord] = (),
    ) -> bool:
        """Persist a new opportunity, its creation event, and its condition mirror.

        The three writes share one transaction: either the row, the event, and
        every condition are committed, or none are. A duplicate create (an
        ``opportunity_id`` that already exists) is an idempotent no-op: the
        stored payload, event, and mirror rows are left untouched and
        ``False`` is returned. An existing opportunity is changed only through
        :meth:`transition_opportunity`, so the payload and the mirror can never
        disagree because of a later "create".
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                payload = json.loads(record.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
            if isinstance(expires_at, str):
                try:
                    expires_at = normalize_timestamp(expires_at)
                except (TypeError, ValueError):
                    # A malformed expiry is stored as absent rather than
                    # compared lexicographically in a different format.
                    _LOGGER.warning(
                        "opportunity_expires_at_unparseable",
                        extra={"opportunity_id": record.opportunity_id},
                    )
                    expires_at = None
            else:
                expires_at = None
            cursor = connection.execute(
                """
                INSERT INTO opportunities (
                    opportunity_id, symbol, state, created_at, updated_at, payload_json,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id) DO NOTHING
                """,
                (
                    record.opportunity_id,
                    record.symbol,
                    record.state,
                    record.created_at,
                    record.updated_at,
                    record.payload_json,
                    expires_at if isinstance(expires_at, str) else None,
                ),
            )
            if cursor.rowcount == 0:
                # The row already exists: do not rewrite the event or the
                # condition mirror. All-or-nothing means a duplicate create
                # changes nothing, not just the row.
                connection.commit()
                return False
            if event is not None:
                # The CREATED row is part of the same unit: the timeline must not
                # lose the creation event once a transition event exists.
                connection.execute(
                    """
                    INSERT OR IGNORE INTO opportunity_events (
                        opportunity_id, seq, at, from_state, to_state, reason_code, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.opportunity_id,
                        event.seq,
                        event.at,
                        event.from_state,
                        event.to_state,
                        event.reason_code,
                        event.note,
                    ),
                )
            for condition in conditions:
                # Reached only for a freshly inserted row, so the mirror always
                # matches the payload exactly. A later re-save with a shorter
                # list is not supported (use a transition, not a create).
                connection.execute(
                    """
                    INSERT OR REPLACE INTO opportunity_conditions (
                        condition_id, opportunity_id, kind, seq, type,
                        params_json, window_start, window_end, required_data_json,
                        evaluable, unavailable_reason, last_value,
                        last_evaluated_at, last_result
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        condition.condition_id,
                        condition.opportunity_id,
                        condition.kind,
                        condition.seq,
                        condition.type,
                        condition.params_json,
                        condition.window_start,
                        condition.window_end,
                        condition.required_data_json,
                        int(condition.evaluable),
                        condition.unavailable_reason,
                        condition.last_value,
                        condition.last_evaluated_at,
                        (None if condition.last_result is None else int(condition.last_result)),
                    ),
                )
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition_opportunity(
        self, record: OpportunityRecord, event: OpportunityEventRecord
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            return self._apply_transition(connection, record, event)

    def _apply_transition(
        self,
        connection: sqlite3.Connection,
        record: OpportunityRecord,
        event: OpportunityEventRecord,
    ) -> bool:
        """Apply the domain ``_ALLOWED_TRANSITIONS`` guard and history event.

        The guard is derived from the domain map so a future transition change
        can never drift from the SQL condition. Callers own the transaction;
        this returns ``False`` when the current stored state is not a legal
        source state (the UPDATE matched no row).
        """
        try:
            target = OpportunityState(record.state)
        except ValueError:
            return False
        allowed_from = tuple(
            sorted(
                source.value
                for source, targets in _ALLOWED_TRANSITIONS.items()
                if target in targets
            )
        )
        if not allowed_from:
            return False
        placeholders = ", ".join("?" for _ in allowed_from)
        cursor = connection.execute(
            f"""
            UPDATE opportunities SET state = ?, updated_at = ?, payload_json = ?
            WHERE opportunity_id = ? AND state IN ({placeholders})
            """,
            (
                record.state,
                record.updated_at,
                record.payload_json,
                record.opportunity_id,
                *allowed_from,
            ),
        )
        if cursor.rowcount == 0:
            return False
        connection.execute(
            """
            INSERT INTO opportunity_events (
                opportunity_id, seq, at, from_state, to_state, reason_code, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.opportunity_id,
                event.seq,
                event.at,
                event.from_state,
                event.to_state,
                event.reason_code,
                event.note,
            ),
        )
        return True

    def get_opportunity(self, opportunity_id: str) -> OpportunityRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_opportunity(row) if row is not None else None

    def list_opportunities(
        self, *, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[OpportunityRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if state is None:
            query = "SELECT * FROM opportunities ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params: tuple[object, ...] = (limit, offset)
        else:
            query = (
                "SELECT * FROM opportunities WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params = (state, limit, offset)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_opportunity(row) for row in rows)

    def list_due_opportunities(
        self, now: str, *, limit: int = 1000
    ) -> tuple[OpportunityRecord, ...]:
        """OPEN rows whose ``expires_at`` has passed, oldest first.

        Selecting in SQL keeps a due row from starving behind newer OPEN rows.
        """
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            normalized_now = normalize_timestamp(now)
            rows = connection.execute(
                "SELECT * FROM opportunities "
                "WHERE state = ? AND expires_at <= ? "
                "ORDER BY expires_at ASC LIMIT ?",
                ("OPEN", normalized_now, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_opportunity(row) for row in rows)

    def count_opportunities(self, *, state: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        try:
            if state is None:
                row = connection.execute("SELECT COUNT(*) AS total FROM opportunities").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM opportunities WHERE state = ?", (state,)
                ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def list_opportunity_events(self, opportunity_id: str) -> tuple[OpportunityEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM opportunity_events WHERE opportunity_id = ? ORDER BY seq",
                (opportunity_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_opportunity_event(row) for row in rows)

    def list_conditions(
        self, opportunity_id: str, kind: str | None = None
    ) -> tuple[ConditionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            if kind is None:
                rows = connection.execute(
                    "SELECT * FROM opportunity_conditions WHERE opportunity_id = ? "
                    "ORDER BY kind, seq",
                    (opportunity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM opportunity_conditions "
                    "WHERE opportunity_id = ? AND kind = ? ORDER BY seq",
                    (opportunity_id, kind),
                ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_condition(row) for row in rows)

    def update_condition_evaluation(
        self,
        opportunity_id: str,
        condition_id: str,
        *,
        last_value: str | None,
        last_evaluated_at: str | None,
        last_result: bool | None,
    ) -> bool:
        # Condition IDs restart per opportunity, so the address is composite.
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE opportunity_conditions
                SET last_value = ?, last_evaluated_at = ?, last_result = ?
                WHERE opportunity_id = ? AND condition_id = ?
                """,
                (
                    last_value,
                    last_evaluated_at,
                    None if last_result is None else int(last_result),
                    opportunity_id,
                    condition_id,
                ),
            )
            if cursor.rowcount == 0:
                _LOGGER.warning(
                    "opportunity_condition_evaluation_missing",
                    extra={
                        "opportunity_id": opportunity_id,
                        "condition_id": condition_id,
                    },
                )
                return False
        return True

    # -- Triggers and risk verdicts --------------------------------------

    def save_trigger_event(self, record: TriggerEventRecord) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO trigger_events (
                    trigger_id, opportunity_id, symbol, status, idempotency_key,
                    triggered_at, payload_json, snapshot_hash, as_of,
                    data_quality, condition_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    record.trigger_id,
                    record.opportunity_id,
                    record.symbol,
                    record.status,
                    record.idempotency_key,
                    record.triggered_at,
                    record.payload_json,
                    record.snapshot_hash,
                    record.as_of,
                    record.data_quality,
                    record.condition_count,
                ),
            )
        return cursor.rowcount == 1

    def save_trigger_and_transition(
        self,
        record: TriggerEventRecord,
        opportunity: OpportunityRecord,
        event: OpportunityEventRecord,
    ) -> Literal["inserted", "existing", "conflict"]:
        connection = self._connect()
        try:
            if connection.in_transaction:
                connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO trigger_events (
                    trigger_id, opportunity_id, symbol, status, idempotency_key,
                    triggered_at, payload_json, snapshot_hash, as_of,
                    data_quality, condition_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    record.trigger_id,
                    record.opportunity_id,
                    record.symbol,
                    record.status,
                    record.idempotency_key,
                    record.triggered_at,
                    record.payload_json,
                    record.snapshot_hash,
                    record.as_of,
                    record.data_quality,
                    record.condition_count,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = connection.execute(
                "SELECT state FROM opportunities WHERE opportunity_id = ?",
                (opportunity.opportunity_id,),
            ).fetchone()
            if stored is None:
                connection.execute("ROLLBACK")
                return "conflict"
            state = str(stored["state"])
            if inserted:
                if not self._apply_transition(connection, opportunity, event):
                    # The opportunity left OPEN between the service read and
                    # this transaction: never leave an orphan trigger row.
                    connection.execute("ROLLBACK")
                    return "conflict"
                connection.execute("COMMIT")
                return "inserted"
            existing = connection.execute(
                "SELECT 1 FROM trigger_events WHERE idempotency_key = ?",
                (record.idempotency_key,),
            ).fetchone()
            if existing is None:
                # The only conflict target is the idempotency key, so this is
                # an unexpected constraint outcome: never report a phantom row.
                connection.execute("ROLLBACK")
                _LOGGER.warning(
                    "trigger_insert_missing_duplicate",
                    extra={
                        "trigger_id": record.trigger_id,
                        "idempotency_key": record.idempotency_key,
                    },
                )
                return "conflict"
            if state == "TRIGGERED":
                connection.execute("COMMIT")
                return "existing"
            if state == "OPEN":
                # Crash replay: the trigger committed but the transition did
                # not. Complete the OPEN -> TRIGGERED move idempotently.
                if not self._apply_transition(connection, opportunity, event):
                    connection.execute("ROLLBACK")
                    return "conflict"
                connection.execute("COMMIT")
                return "existing"
            connection.execute("ROLLBACK")
            return "conflict"
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_trigger_event(
        self,
        record: TriggerEventRecord,
        *,
        expected_status: str | None = None,
        require_no_order: bool = False,
        require_no_entry_order: bool = False,
    ) -> bool:
        clauses = ["trigger_id = ?"]
        params: list[object] = [record.status, record.payload_json, record.trigger_id]
        if expected_status is not None:
            clauses.append("status = ?")
            params.append(expected_status)
        if require_no_order:
            # ``created_order_id`` is a payload field (the frozen trigger
            # contract), not a column; the compare-and-set reads it from JSON.
            clauses.append("json_extract(payload_json, '$.created_order_id') IS NULL")
        if require_no_entry_order:
            # Atomic with the update: a concurrent accept that committed its
            # order before this statement makes the write lose.
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM orders o WHERE o.trigger_id = trigger_events.trigger_id "
                "AND o.purpose = 'ENTRY' AND o.environment = 'PAPER')"
            )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"""
                UPDATE trigger_events SET status = ?, payload_json = ?
                WHERE {" AND ".join(clauses)}
                """,
                tuple(params),
            )
        return cursor.rowcount == 1

    def get_trigger_event(self, trigger_id: str) -> TriggerEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trigger_events WHERE trigger_id = ?", (trigger_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trigger_event(row) if row is not None else None

    def get_trigger_by_idempotency(self, idempotency_key: str) -> TriggerEventRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM trigger_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_trigger_event(row) if row is not None else None

    def list_trigger_events(
        self,
        *,
        opportunity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[TriggerEventRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        clauses: list[str] = []
        params: list[object] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(opportunity_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT * FROM trigger_events{where} "
            "ORDER BY triggered_at DESC, trigger_id DESC LIMIT ? OFFSET ?"
        )
        params.extend((limit, offset))
        try:
            rows = connection.execute(query, tuple(params)).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_trigger_event(row) for row in rows)

    def count_trigger_events(
        self, *, opportunity_id: str | None = None, status: str | None = None
    ) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        clauses: list[str] = []
        params: list[object] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(opportunity_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM trigger_events{where}", tuple(params)
            ).fetchone()
        finally:
            connection.close()
        return int(row["total"]) if row is not None else 0

    def save_risk_verdict(self, record: RiskVerdictRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO risk_verdicts (
                    risk_verdict_id, trigger_id, decision_id, verdict, binding_cap,
                    computed_at, expires_at, payload_json, spread_bps,
                    expected_slippage_bps, daily_loss_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(risk_verdict_id) DO UPDATE SET
                    trigger_id = excluded.trigger_id,
                    decision_id = excluded.decision_id,
                    verdict = excluded.verdict,
                    binding_cap = excluded.binding_cap,
                    computed_at = excluded.computed_at,
                    expires_at = excluded.expires_at,
                    payload_json = excluded.payload_json,
                    spread_bps = excluded.spread_bps,
                    expected_slippage_bps = excluded.expected_slippage_bps,
                    daily_loss_usd = excluded.daily_loss_usd
                """,
                (
                    record.risk_verdict_id,
                    record.trigger_id,
                    record.decision_id,
                    record.verdict,
                    record.binding_cap,
                    record.computed_at,
                    record.expires_at,
                    record.payload_json,
                    record.spread_bps,
                    record.expected_slippage_bps,
                    record.daily_loss_usd,
                ),
            )

    def get_risk_verdict(self, risk_verdict_id: str) -> RiskVerdictRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM risk_verdicts WHERE risk_verdict_id = ?",
                (risk_verdict_id,),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_risk_verdict(row) if row is not None else None

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


def _row_to_opportunity(row: sqlite3.Row) -> OpportunityRecord:
    return OpportunityRecord(
        opportunity_id=row["opportunity_id"],
        symbol=row["symbol"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload_json=row["payload_json"],
    )


def _row_to_opportunity_event(row: sqlite3.Row) -> OpportunityEventRecord:
    return OpportunityEventRecord(
        opportunity_id=row["opportunity_id"],
        seq=row["seq"],
        at=row["at"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        reason_code=row["reason_code"],
        note=row["note"],
    )


def _row_to_condition(row: sqlite3.Row) -> ConditionRecord:
    return ConditionRecord(
        condition_id=row["condition_id"],
        opportunity_id=row["opportunity_id"],
        kind=row["kind"],
        seq=row["seq"],
        type=row["type"],
        params_json=row["params_json"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        required_data_json=row["required_data_json"],
        evaluable=bool(row["evaluable"]),
        unavailable_reason=row["unavailable_reason"],
        last_value=row["last_value"],
        last_evaluated_at=row["last_evaluated_at"],
        last_result=None if row["last_result"] is None else bool(row["last_result"]),
    )


def _row_to_trigger_event(row: sqlite3.Row) -> TriggerEventRecord:
    return TriggerEventRecord(
        trigger_id=row["trigger_id"],
        opportunity_id=row["opportunity_id"],
        symbol=row["symbol"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        triggered_at=row["triggered_at"],
        payload_json=row["payload_json"],
        snapshot_hash=row["snapshot_hash"],
        as_of=row["as_of"],
        data_quality=row["data_quality"],
        condition_count=int(row["condition_count"]),
    )


def _row_to_risk_verdict(row: sqlite3.Row) -> RiskVerdictRecord:
    keys = row.keys()
    return RiskVerdictRecord(
        risk_verdict_id=row["risk_verdict_id"],
        trigger_id=row["trigger_id"],
        decision_id=row["decision_id"],
        verdict=row["verdict"],
        binding_cap=row["binding_cap"],
        computed_at=row["computed_at"],
        expires_at=row["expires_at"],
        payload_json=row["payload_json"],
        spread_bps=row["spread_bps"] if "spread_bps" in keys else None,
        expected_slippage_bps=(
            row["expected_slippage_bps"] if "expected_slippage_bps" in keys else None
        ),
        daily_loss_usd=row["daily_loss_usd"] if "daily_loss_usd" in keys else None,
    )


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
