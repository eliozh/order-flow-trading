"""Bounded report reads: symbol pages, counts, and per-timeframe headers.

Split out of the scan store so that module stays under the maintainability
ceiling. Every read here is SQL-bounded: the page query applies the symbol /
scan filter, ordering, and ``LIMIT``/``OFFSET`` in SQLite, the count query
aggregates in SQLite, and the header projection uses a window function so at
most one small row per timeframe crosses the boundary. None of these methods
selects a full frozen document.
"""

from __future__ import annotations

from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.adapters.sqlite_scan_store import _row_to_report
from oft.ports.store import ReportHeader, ReportRecord


class ReportReadsMixin(SqliteConnectionMixin):
    """Read bounded report pages, counts, and newest-per-timeframe headers."""

    def list_reports_for_symbol_page(
        self, symbol: str, *, scan_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[ReportRecord, ...]:
        """One bounded newest-first page of one symbol's reports (SQL LIMIT/OFFSET)."""
        connection = self._connect_read_only()
        if connection is None:
            return ()
        clauses = ["symbol = ?"]
        params: list[object] = [symbol]
        if scan_id is not None:
            clauses.append("scan_id = ?")
            params.append(scan_id)
        params.extend((limit, offset))
        try:
            rows = connection.execute(
                f"SELECT * FROM reports WHERE {' AND '.join(clauses)} "
                "ORDER BY generated_at DESC, report_id DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_report(row) for row in rows)

    def count_reports_for_symbol(self, symbol: str, *, scan_id: str | None = None) -> int:
        connection = self._connect_read_only()
        if connection is None:
            return 0
        clauses = ["symbol = ?"]
        params: list[object] = [symbol]
        if scan_id is not None:
            clauses.append("scan_id = ?")
            params.append(scan_id)
        try:
            row = connection.execute(
                f"SELECT COUNT(*) FROM reports WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def list_report_headers_for_symbol(self, symbol: str) -> tuple[ReportHeader, ...]:
        """Newest header per timeframe for a symbol; four small columns only.

        The window function partitions inside SQLite, so at most one row per
        timeframe crosses the boundary and the full frozen documents are never
        selected. ``reports_symbol_generated`` backs the symbol range scan.
        """
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT report_id, symbol, timeframe, generated_at FROM ("
                "  SELECT report_id, symbol, timeframe, generated_at,"
                "    ROW_NUMBER() OVER ("
                "      PARTITION BY timeframe ORDER BY generated_at DESC, report_id DESC"
                "    ) AS recency_rank"
                "  FROM reports WHERE symbol = ?"
                ") WHERE recency_rank = 1"
                " ORDER BY generated_at DESC, report_id DESC",
                (symbol,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            ReportHeader(
                report_id=row["report_id"],
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                generated_at=row["generated_at"],
            )
            for row in rows
        )
