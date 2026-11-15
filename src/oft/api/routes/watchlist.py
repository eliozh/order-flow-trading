"""Watchlist surface: grouped registry rows plus star/unstar mutations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.ports.store import StarState

router = APIRouter()

_WATCHLIST_ROW_CAP = 2000
_UNKNOWN_STAR = StarState(starred=False, starred_at=None)


def _registry(request: Request) -> Any:
    return request.app.state.container.symbol_registry


def _star_payload(symbol: str, starred: bool, starred_at: str | None) -> dict[str, Any]:
    return {"symbol": symbol, "starred": starred, "starred_at": starred_at}


@router.get("/api/v1/watchlist")
def watchlist(request: Request) -> dict[str, Any]:
    """Group every registry contract and additively expose its star state.

    ``groups`` stays the full registry view (name/count/symbols as strings).
    ``rows`` carries the full ``SymbolContract`` JSON plus ``starred`` and
    ``starred_at`` for each bounded row. ``next_scan_count`` is read from the
    starred set itself, so it always agrees with ``registry.list_starred()``.
    """
    registry = _registry(request)
    # One bounded read instead of two full-table reads.
    contracts = registry.list_symbols(limit=_WATCHLIST_ROW_CAP)
    states = registry.star_states()
    crypto = [contract for contract in contracts if not contract.tradifi]
    tradfi = [contract for contract in contracts if contract.tradifi]

    rows: list[dict[str, Any]] = []
    for contract in contracts:
        state = states.get(contract.symbol, _UNKNOWN_STAR)
        row = contract.model_dump(mode="json")
        row["starred"] = state.starred
        row["starred_at"] = state.starred_at
        rows.append(row)

    next_scan_count = len(registry.list_starred())
    return success_envelope(
        {
            "groups": [
                {
                    "name": "CRYPTO",
                    "count": len(crypto),
                    "symbols": [contract.symbol for contract in crypto],
                },
                {
                    "name": "TRADFI",
                    "count": len(tradfi),
                    "symbols": [contract.symbol for contract in tradfi],
                },
            ],
            "total": len(crypto) + len(tradfi),
            "rows": rows,
            "starred_count": next_scan_count,
            "next_scan_count": next_scan_count,
            "star_mutation": True,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if crypto or tradfi else "MISSING",
    )


def _change_star(request: Request, symbol: str, starred: bool) -> dict[str, Any]:
    result = _registry(request).set_star_with_audit(symbol, starred, actor="web")
    if result is None:
        raise ApiError("NOT_FOUND", f"Symbol {symbol} does not exist.")
    return success_envelope(
        _star_payload(result.symbol, result.after.starred, result.after.starred_at),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


@router.post("/api/v1/watchlist/{symbol}")
def star_watchlist_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Star one registry symbol (semantically idempotent, audited).

    No ``Idempotency-Key`` is required. Starring an already-starred symbol
    keeps the original ``starred_at`` and writes no audit event; only the
    transition from unstarred to starred updates the row and appends exactly
    one ``WATCHLIST_STAR_TOGGLED`` event.
    """
    return _change_star(request, symbol, True)


@router.delete("/api/v1/watchlist/{symbol}")
def unstar_watchlist_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Unstar one registry symbol (semantically idempotent, audited).

    Unstarring an already-unstarred symbol is a no-op: no row update and no
    audit event. An actual unstar clears ``starred_at`` and appends exactly one
    ``WATCHLIST_STAR_TOGGLED`` event with the before/after state.
    """
    return _change_star(request, symbol, False)
