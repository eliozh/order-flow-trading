"""Symbol registry mixin.

Covers the ``symbols`` table plus its row mapper and value helpers.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, suppress
from decimal import Decimal
from typing import Any

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.adapters.sqlite_schema import _SYMBOLS_COLUMNS
from oft.domain.symbols import SymbolContract
from oft.ports.store import (
    AuditEventDraft,
    StarChangeResult,
    StarState,
    StoreUnavailableError,
    SymbolUpsertResult,
    WatchlistEntry,
    WatchlistSnapshot,
)


def _like_pattern(query: str) -> str:
    """Escape LIKE metacharacters so a literal ``_``/``%`` does not match all."""
    escaped = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _count(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row is not None else 0


_SYMBOL_SPEC_COLUMNS = _SYMBOLS_COLUMNS[:-1]


class SymbolStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write symbols and stars."""

    def upsert_symbols(
        self, contracts: tuple[SymbolContract, ...], *, observed_at: str
    ) -> SymbolUpsertResult:
        """Insert or update contracts; ``updated_at`` moves only when the spec changed."""
        inserted = updated = unchanged = 0
        columns = ", ".join(_SYMBOLS_COLUMNS)
        placeholders = ", ".join("?" for _ in _SYMBOLS_COLUMNS)
        assignments = ", ".join(
            f"{column} = excluded.{column}" for column in _SYMBOL_SPEC_COLUMNS[1:]
        )
        upsert_sql = (
            f"INSERT INTO symbols ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {assignments}, "
            "updated_at = excluded.updated_at"
        )
        connection = None
        try:
            connection = self._connect()
            for contract in contracts:
                row = connection.execute(
                    "SELECT * FROM symbols WHERE symbol = ?", (contract.symbol,)
                ).fetchone()
                if row is not None and _row_to_symbol(row) == contract:
                    unchanged += 1
                    continue
                connection.execute(upsert_sql, (*_symbol_values(contract), observed_at))
                if row is None:
                    inserted += 1
                else:
                    updated += 1
            total_row = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise StoreUnavailableError(f"symbol registry write failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
        return SymbolUpsertResult(
            inserted=inserted, updated=updated, unchanged=unchanged, total=total
        )

    def _symbol_query(
        self,
        *,
        query: str | None,
        tradifi: bool | None,
        contract_type: str | None,
        starred: bool | None = None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if query:
            pattern = _like_pattern(query)
            clauses.append(
                "(LOWER(symbol) LIKE ? ESCAPE '\\' OR LOWER(base_asset) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])
        if tradifi is not None:
            clauses.append("tradifi = ?")
            params.append(int(tradifi))
        if contract_type:
            clauses.append("contract_type = ?")
            params.append(contract_type)
        if starred is not None:
            clauses.append("starred = ?")
            params.append(int(starred))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        starred: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type, starred=starred
        )
        if query:
            order = " ORDER BY CASE WHEN symbol = ? THEN 0 ELSE 1 END, tradifi ASC, symbol ASC"
            params.append(query.strip().upper())
        else:
            order = " ORDER BY tradifi ASC, symbol ASC"
        sql = f"SELECT * FROM symbols{where}{order}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        try:
            rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol registry read failed: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_symbol(row) for row in rows)

    def count_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        starred: bool | None = None,
    ) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        where, params = self._symbol_query(
            query=query, tradifi=tradifi, contract_type=contract_type, starred=starred
        )
        try:
            row = connection.execute(f"SELECT COUNT(*) FROM symbols{where}", params).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol registry count failed: {exc}") from exc
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def get_symbol(self, symbol: str) -> SymbolContract | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"symbol lookup failed: {exc}") from exc
        finally:
            connection.close()
        return _row_to_symbol(row) if row is not None else None

    def set_starred(self, symbol: str, starred: bool, *, starred_at: str | None = None) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE symbols SET starred = ?, starred_at = ? WHERE symbol = ?",
                (int(starred), starred_at if starred else None, symbol),
            )
            return cursor.rowcount > 0

    def _star_state(self, row: sqlite3.Row) -> StarState:
        return StarState(starred=bool(row["starred"]), starred_at=row["starred_at"])

    def set_starred_with_audit(
        self,
        symbol: str,
        starred: bool,
        *,
        starred_at: str | None,
        actor: dict[str, Any],
        created_at: str,
        event_type: str = "WATCHLIST_STAR_TOGGLED",
    ) -> StarChangeResult | None:
        """Set the star flag and append its audit event in one transaction.

        A semantic no-op (the flag already has the requested value) leaves the
        row untouched, keeps the original ``starred_at``, and writes no audit
        event, so a replayed POST/DELETE can never duplicate stars or audits.
        """
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT starred, starred_at FROM symbols WHERE symbol = ?", (symbol,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            before = self._star_state(row)
            if before.starred == starred:
                connection.rollback()
                return StarChangeResult(symbol=symbol, before=before, after=before, changed=False)
            after = StarState(starred=starred, starred_at=starred_at if starred else None)
            connection.execute(
                "UPDATE symbols SET starred = ?, starred_at = ? WHERE symbol = ?",
                (int(starred), after.starred_at, symbol),
            )
            draft = AuditEventDraft(
                event_type=event_type,
                actor=actor,
                subject={"kind": "SYMBOL", "id": symbol},
                payload={
                    "symbol": symbol,
                    "before": {
                        "starred": before.starred,
                        "starred_at": before.starred_at,
                    },
                    "after": {"starred": after.starred, "starred_at": after.starred_at},
                },
                created_at=created_at,
            )
            self._insert_audit_event(connection, draft)
            connection.commit()
            return StarChangeResult(symbol=symbol, before=before, after=after, changed=True)
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise StoreUnavailableError(f"star update failed: {exc}") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def get_star_state(self, symbol: str) -> StarState | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT starred, starred_at FROM symbols WHERE symbol = ?", (symbol,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"star lookup failed: {exc}") from exc
        finally:
            connection.close()
        return self._star_state(row) if row is not None else None

    def star_states(self) -> dict[str, StarState]:
        connection = self._connect_read_only()
        if connection is None:
            return {}
        try:
            rows = connection.execute("SELECT symbol, starred, starred_at FROM symbols").fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"star-state read failed: {exc}") from exc
        finally:
            connection.close()
        return {str(row["symbol"]): self._star_state(row) for row in rows}

    def list_starred(self) -> tuple[SymbolContract, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM symbols WHERE starred = 1 ORDER BY symbol"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"starred symbol read failed: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_symbol(row) for row in rows)

    def watchlist_snapshot(self, *, limit: int) -> WatchlistSnapshot:
        """One read transaction: bounded rows plus true registry counts.

        A ``BEGIN`` read transaction makes the row page and the ``COUNT(*)``
        values one consistent snapshot, so a concurrent ingest or star change
        cannot make the counts disagree with the rows they accompany. Returns
        an empty snapshot when the database does not exist yet (read-only
        diagnostics never create it).
        """
        connection = self._connect_read_only()
        if connection is None:
            return WatchlistSnapshot(
                entries=(), total=0, crypto_count=0, tradfi_count=0, starred_count=0
            )
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM symbols ORDER BY tradifi ASC, symbol ASC LIMIT ?", (limit,)
            ).fetchall()
            total = _count(connection, "SELECT COUNT(*) FROM symbols")
            crypto_count = _count(connection, "SELECT COUNT(*) FROM symbols WHERE tradifi = 0")
            tradfi_count = _count(connection, "SELECT COUNT(*) FROM symbols WHERE tradifi = 1")
            starred_count = _count(connection, "SELECT COUNT(*) FROM symbols WHERE starred = 1")
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise StoreUnavailableError(f"watchlist snapshot read failed: {exc}") from exc
        finally:
            connection.close()
        entries = tuple(
            WatchlistEntry(
                contract=_row_to_symbol(row),
                starred=bool(row["starred"]),
                starred_at=row["starred_at"],
            )
            for row in rows
        )
        return WatchlistSnapshot(
            entries=entries,
            total=total,
            crypto_count=crypto_count,
            tradfi_count=tradfi_count,
            starred_count=starred_count,
        )


def _row_to_symbol(row: sqlite3.Row) -> SymbolContract:
    return SymbolContract(
        symbol=row["symbol"],
        base_asset=row["base_asset"],
        quote_asset=row["quote_asset"],
        contract_type=row["contract_type"],
        underlying_type=row["underlying_type"],
        status=row["status"],
        tick_size=Decimal(row["tick_size"]),
        step_size=Decimal(row["step_size"]),
        min_qty=Decimal(row["min_qty"]),
        min_notional=Decimal(row["min_notional"]),
        price_precision=row["price_precision"],
        quantity_precision=row["quantity_precision"],
        onboard_date=row["onboard_date"],
        tradifi=bool(row["tradifi"]),
    )


def _symbol_values(contract: SymbolContract) -> tuple[object, ...]:
    return (
        contract.symbol,
        contract.base_asset,
        contract.quote_asset,
        contract.contract_type,
        contract.underlying_type,
        contract.status,
        str(contract.tick_size),
        str(contract.step_size),
        str(contract.min_qty),
        str(contract.min_notional),
        contract.price_precision,
        contract.quantity_precision,
        contract.onboard_date,
        int(contract.tradifi),
    )
