"""Symbol registry mixin.

Covers the ``symbols`` table plus its row mapper and value helpers.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.adapters.sqlite_schema import _SYMBOLS_COLUMNS
from oft.domain.symbols import SymbolContract
from oft.ports.store import (
    StoreUnavailableError,
    SymbolUpsertResult,
)


def _like_pattern(query: str) -> str:
    """Escape LIKE metacharacters so a literal ``_``/``%`` does not match all."""
    escaped = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
