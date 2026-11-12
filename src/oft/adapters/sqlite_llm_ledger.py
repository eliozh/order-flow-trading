"""LLM ledger mixin: provider calls, decisions, and the budget ledger.

Covers the ``llm_calls``, ``decisions``, ``budget_ledger``, and
``budget_settings`` tables plus their row mappers.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.domain.budget_periods import budget_record_id
from oft.ports.store import (
    AuditEventDraft,
    BudgetRecord,
    BudgetRecordConflictError,
    BudgetSettingsRecord,
    DecisionRecord,
    LLMCallRecord,
)


def _zero_budget_totals() -> dict[str, dict[str, Any]]:
    return {
        "day": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
        "week": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
        "month": {"spend_usd": "0.00000000", "call_count": 0, "unpriced_count": 0},
    }


class LLMStoreMixin(SqliteAuditMixin):
    """Read and write LLM calls, decisions, and budget rows."""

    if TYPE_CHECKING:

        def _connect(self) -> sqlite3.Connection: ...
        def _connect_read_only(self) -> sqlite3.Connection | None: ...

    # -- LLM decision ledger ---------------------------------------------

    def save_llm_call(self, record: LLMCallRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._insert_llm_call(connection, record)

    def _insert_llm_call(self, connection: sqlite3.Connection, record: LLMCallRecord) -> None:
        connection.execute(
            """
            INSERT INTO llm_calls (
                call_id, decision_id, role, provider, model, prompt_id,
                prompt_version, prompt_hash, input_hash, attempt, request_id,
                status, raw_output_hash, usage_json, latency_ms, error_code, created_at,
                system_prompt_id, system_prompt_version, system_prompt_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(call_id) DO NOTHING
            """,
            (
                record.call_id,
                record.decision_id,
                record.role,
                record.provider,
                record.model,
                record.prompt_id,
                record.prompt_version,
                record.prompt_hash,
                record.input_hash,
                record.attempt,
                record.request_id,
                record.status,
                record.raw_output_hash,
                record.usage_json,
                record.latency_ms,
                record.error_code,
                record.created_at,
                record.system_prompt_id,
                record.system_prompt_version,
                record.system_prompt_hash,
            ),
        )

    def list_llm_calls(
        self, *, decision_id: str | None = None, role: str | None = None
    ) -> tuple[LLMCallRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        clauses: list[str] = []
        params: list[object] = []
        if decision_id is not None:
            clauses.append("decision_id = ?")
            params.append(decision_id)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = connection.execute(
                f"SELECT * FROM llm_calls {where} ORDER BY created_at, attempt", tuple(params)
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_llm_call(row) for row in rows)

    def save_decision(self, record: DecisionRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, role, decision_type, validation_status,
                    raw_output_hash, persisted_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO NOTHING
                """,
                (
                    record.decision_id,
                    record.role,
                    record.decision_type,
                    record.validation_status,
                    record.raw_output_hash,
                    record.persisted_at,
                    record.payload_json,
                ),
            )

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_decision(row) if row is not None else None

    def list_decisions(self, *, role: str | None = None) -> tuple[DecisionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if role is None:
            query = "SELECT * FROM decisions ORDER BY persisted_at"
            params: tuple[object, ...] = ()
        else:
            query = "SELECT * FROM decisions WHERE role = ? ORDER BY persisted_at"
            params = (role,)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_decision(row) for row in rows)

    # -- Budget ledger ---------------------------------------------------

    def _insert_budget_record(self, connection: sqlite3.Connection, record: BudgetRecord) -> int:
        cursor = connection.execute(
            """
            INSERT INTO budget_ledger (
                record_id, call_id, decision_id, occurred_at, provider, model,
                cost_usd, price_version, unpriced, budget_state, period_day,
                period_week, period_month, role, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO NOTHING
            """,
            (
                record.record_id,
                record.call_id,
                record.decision_id,
                record.occurred_at,
                record.provider,
                record.model,
                record.cost_usd,
                record.price_version,
                int(record.unpriced),
                record.budget_state,
                record.period_day,
                record.period_week,
                record.period_month,
                record.role,
                record.payload_json,
            ),
        )
        return cursor.rowcount

    def save_budget_record(self, record: BudgetRecord) -> bool:
        """Insert one ledger row; return whether it was actually written.

        ``BEGIN IMMEDIATE`` serializes concurrent writers. A duplicate
        ``record_id`` is an idempotent no-op and reports ``False``.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._insert_budget_record(connection, record) > 0
            connection.execute("COMMIT")
            return inserted
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _allocate_call_id(self, connection: sqlite3.Connection, base: str) -> str:
        """A call id that does not collide in either ledger table (M2/M3)."""
        candidate = base
        while self._call_id_exists(connection, candidate):
            candidate = f"{base}-{uuid.uuid4().hex[:8].upper()}"
        return candidate

    @staticmethod
    def _call_id_exists(connection: sqlite3.Connection, call_id: str) -> bool:
        for table in ("llm_calls", "budget_ledger"):
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE call_id = ? LIMIT 1", (call_id,)
            ).fetchone()
            if row is not None:
                return True
        return False

    @staticmethod
    def _rewrite_budget_payload(
        payload_json: str, *, call_id: str, record_id: str, budget_state: str
    ) -> str:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return payload_json
        if not isinstance(payload, dict):
            return payload_json
        payload["call_id"] = call_id
        payload["record_id"] = record_id
        payload["budget_state"] = budget_state
        return json.dumps(payload, sort_keys=True)

    def save_call_and_budget(
        self,
        call: LLMCallRecord,
        budget: BudgetRecord,
        compute: Callable[[dict[str, dict[str, Any]]], tuple[str, list[AuditEventDraft]]],
    ) -> tuple[LLMCallRecord, BudgetRecord]:
        """Atomically write the call row, budget row, and threshold audit (M2).

        The whole read-compute-write cycle runs under one ``BEGIN IMMEDIATE``:
        the prior totals, the state/crossing decision, both inserts, and the
        audit commit together. Cross-process writers therefore cannot split the
        rows or miss a crossing.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            final_call = call
            final_budget = budget
            if self._call_id_exists(connection, call.call_id):
                allocated = self._allocate_call_id(connection, call.call_id)
                final_call = replace(call, call_id=allocated)
                final_budget = replace(
                    budget,
                    call_id=allocated,
                    record_id=budget_record_id(allocated),
                )
            else:
                allocated = call.call_id
            prior = self._budget_totals_for(
                connection,
                day=final_budget.period_day,
                week=final_budget.period_week,
                month=final_budget.period_month,
            )
            state, audits = compute(prior)
            final_budget = replace(
                final_budget,
                budget_state=state,
                payload_json=self._rewrite_budget_payload(
                    final_budget.payload_json,
                    call_id=allocated,
                    record_id=final_budget.record_id,
                    budget_state=state,
                ),
            )
            self._insert_llm_call(connection, final_call)
            inserted_budget = self._insert_budget_record(connection, final_budget)
            if inserted_budget <= 0:
                # A colliding ``record_id`` makes the budget insert a no-op.
                # Raise inside the transaction so the call row rolls back too;
                # returning here would hand the caller a phantom entry.
                raise BudgetRecordConflictError(
                    f"budget record {final_budget.record_id!r} was not persisted"
                )
            for audit in audits:
                # The id may have been reallocated after the caller built the
                # draft; point every audit at the row that actually committed.
                self._insert_audit_event(
                    connection,
                    replace(
                        audit,
                        subject={**audit.subject, "id": final_budget.record_id},
                    ),
                )
            connection.execute("COMMIT")
            return final_call, final_budget
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_budget_records(self, *, decision_id: str | None = None) -> tuple[BudgetRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        if decision_id is None:
            query = "SELECT * FROM budget_ledger ORDER BY occurred_at"
            params: tuple[object, ...] = ()
        else:
            query = "SELECT * FROM budget_ledger WHERE decision_id = ? ORDER BY occurred_at"
            params = (decision_id,)
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_budget(row) for row in rows)

    def last_budget_record(self) -> BudgetRecord | None:
        """The most recent ledger row, without loading the table (M24)."""
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM budget_ledger ORDER BY occurred_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_budget(row)

    def list_budget_records_for_period(self, period: str, key: str) -> tuple[BudgetRecord, ...]:
        """Rows bucketed in one period, filtered in SQL rather than Python (M24)."""
        columns = {"day": "period_day", "week": "period_week", "month": "period_month"}
        column = columns.get(period)
        if column is None:
            raise ValueError("period must be day, week, or month")
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                f"SELECT * FROM budget_ledger WHERE {column} = ? ORDER BY occurred_at",
                (key,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_budget(row) for row in rows)

    def budget_totals(self, *, day: str, week: str, month: str) -> dict[str, dict[str, Any]]:
        connection = self._connect_read_only()
        if connection is None:
            return _zero_budget_totals()
        try:
            return self._budget_totals_for(connection, day=day, week=week, month=month)
        finally:
            connection.close()

    @staticmethod
    def _budget_totals_for(
        connection: sqlite3.Connection, *, day: str, week: str, month: str
    ) -> dict[str, dict[str, Any]]:
        columns = {"day": "period_day", "week": "period_week", "month": "period_month"}
        keys = {"day": day, "week": week, "month": month}
        quantum = Decimal("0.00000001")
        totals: dict[str, dict[str, Any]] = {}
        for name, column in columns.items():
            rows = connection.execute(
                f"""
                SELECT cost_usd, unpriced
                FROM budget_ledger WHERE {column} = ?
                """,
                (keys[name],),
            ).fetchall()
            # Exact Decimal accumulation: SQLite REAL sums drift at 8 dp.
            spend = Decimal("0")
            unpriced_count = 0
            for row in rows:
                try:
                    spend += Decimal(str(row[0]))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if int(row[1]) == 1:
                    unpriced_count += 1
            totals[name] = {
                "spend_usd": format(spend.quantize(quantum, rounding=ROUND_HALF_UP), "f"),
                "call_count": len(rows),
                "unpriced_count": unpriced_count,
            }
        return totals

    def get_budget_settings(self) -> BudgetSettingsRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM budget_settings WHERE id = 'default'"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return BudgetSettingsRecord(
            id=row["id"],
            daily_usd=row["daily_usd"],
            weekly_usd=row["weekly_usd"],
            monthly_usd=row["monthly_usd"],
            warn_pct=float(row["warn_pct"]),
            updated_at=row["updated_at"],
        )

    def save_budget_settings(self, record: BudgetSettingsRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO budget_settings
                    (id, daily_usd, weekly_usd, monthly_usd, warn_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    daily_usd = excluded.daily_usd,
                    weekly_usd = excluded.weekly_usd,
                    monthly_usd = excluded.monthly_usd,
                    warn_pct = excluded.warn_pct,
                    updated_at = excluded.updated_at
                """,
                (
                    "default",
                    record.daily_usd,
                    record.weekly_usd,
                    record.monthly_usd,
                    record.warn_pct,
                    record.updated_at,
                ),
            )


def _row_to_llm_call(row: sqlite3.Row) -> LLMCallRecord:
    keys = row.keys()
    return LLMCallRecord(
        call_id=row["call_id"],
        decision_id=row["decision_id"],
        role=row["role"],
        provider=row["provider"],
        model=row["model"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
        prompt_hash=row["prompt_hash"],
        input_hash=row["input_hash"],
        attempt=row["attempt"],
        request_id=row["request_id"],
        status=row["status"],
        raw_output_hash=row["raw_output_hash"],
        usage_json=row["usage_json"],
        latency_ms=row["latency_ms"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        system_prompt_id=row["system_prompt_id"] if "system_prompt_id" in keys else "",
        system_prompt_version=(
            row["system_prompt_version"] if "system_prompt_version" in keys else 1
        ),
        system_prompt_hash=row["system_prompt_hash"] if "system_prompt_hash" in keys else "",
    )


def _row_to_decision(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=row["decision_id"],
        role=row["role"],
        decision_type=row["decision_type"],
        validation_status=row["validation_status"],
        raw_output_hash=row["raw_output_hash"],
        persisted_at=row["persisted_at"],
        payload_json=row["payload_json"],
    )


def _row_to_budget(row: sqlite3.Row) -> BudgetRecord:
    keys = row.keys()
    return BudgetRecord(
        record_id=row["record_id"],
        call_id=row["call_id"],
        decision_id=row["decision_id"],
        occurred_at=row["occurred_at"],
        provider=row["provider"],
        model=row["model"],
        cost_usd=row["cost_usd"],
        price_version=row["price_version"],
        unpriced=bool(row["unpriced"]),
        budget_state=row["budget_state"],
        period_day=row["period_day"],
        payload_json=row["payload_json"],
        period_week=row["period_week"] if "period_week" in keys else "",
        period_month=row["period_month"] if "period_month" in keys else "",
        role=row["role"] if "role" in keys else "",
    )
