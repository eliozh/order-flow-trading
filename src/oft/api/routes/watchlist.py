"""Watchlist surface: grouped registry rows plus star/unstar mutations.

``GET /api/v1/watchlist`` returns one transaction's view of the registry.
``rows`` (and the ``symbols`` lists inside ``groups``) are bounded to
``_WATCHLIST_ROW_CAP``, while ``total``, each group's ``count``, and
``next_scan_count`` are true registry totals read in that same snapshot, so a
capped response is honest about what it does not show: ``truncated`` is
``True`` whenever ``total`` exceeds the number of returned rows.

``POST``/``DELETE /api/v1/watchlist/{symbol}`` star/unstar one symbol through
the audited star write.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.ports.store import WatchlistSnapshot

router = APIRouter()

_WATCHLIST_ROW_CAP = 2000


def _registry(request: Request) -> Any:
    return request.app.state.container.symbol_registry


def _star_payload(symbol: str, starred: bool, starred_at: str | None) -> dict[str, Any]:
    return {"symbol": symbol, "starred": starred, "starred_at": starred_at}


def _group(name: str, snapshot: WatchlistSnapshot, *, tradifi: bool) -> dict[str, Any]:
    return {
        "name": name,
        # ``count`` is the true group total; ``symbols`` is the bounded page.
        "count": snapshot.tradfi_count if tradifi else snapshot.crypto_count,
        "symbols": [
            entry.contract.symbol for entry in snapshot.entries if entry.contract.tradifi == tradifi
        ],
    }


@router.get("/api/v1/watchlist")
def watchlist(request: Request) -> dict[str, Any]:
    """Return the bounded watchlist page with true registry totals.

    ``SymbolRegistry.watchlist_snapshot`` reads the bounded rows and the
    ``COUNT(*)`` values inside one read transaction, so a concurrent ingest or
    star change cannot make ``groups[].count``/``total``/``next_scan_count``
    disagree with the row ``starred`` flags. ``groups[].symbols`` lists only
    the bounded rows while ``groups[].count`` is the true group total;
    ``truncated`` reports that the page is not the whole registry, and
    ``data_quality`` degrades to ``PARTIAL``.
    """
    snapshot = _registry(request).watchlist_snapshot(limit=_WATCHLIST_ROW_CAP)
    rows: list[dict[str, Any]] = []
    for entry in snapshot.entries:
        row = entry.contract.model_dump(mode="json")
        row["starred"] = entry.starred
        row["starred_at"] = entry.starred_at
        rows.append(row)

    truncated = snapshot.total > len(rows)
    if truncated:
        data_quality = "PARTIAL"
    elif snapshot.total:
        data_quality = "COMPLETE"
    else:
        data_quality = "MISSING"
    return success_envelope(
        {
            "groups": [
                _group("CRYPTO", snapshot, tradifi=False),
                _group("TRADFI", snapshot, tradifi=True),
            ],
            "total": snapshot.total,
            "rows": rows,
            "next_scan_count": snapshot.starred_count,
            "truncated": truncated,
            "star_mutation": True,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality=data_quality,
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

    Starring an already-starred symbol keeps the original ``starred_at`` and
    writes no audit event; only the transition from unstarred to starred
    updates the row and appends exactly one ``WATCHLIST_STAR_TOGGLED`` event.

    ``Idempotency-Key`` is not required: this route is idempotent by
    construction (a replay is a no-op with one audit per real change), so a
    stored response adds nothing. This is the documented carve-out from
    ADR-0003's blanket rule for state-creating mutations; the Web client still
    sends a key for consistency with every other mutation.
    """
    return _change_star(request, symbol, True)


@router.delete("/api/v1/watchlist/{symbol}")
def unstar_watchlist_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Unstar one registry symbol (semantically idempotent, audited).

    Unstarring an already-unstarred symbol is a no-op: no row update and no
    audit event. An actual unstar clears ``starred_at`` and appends exactly one
    ``WATCHLIST_STAR_TOGGLED`` event with the before/after state.

    ``Idempotency-Key`` is not required: a replay of the same unstar is a
    no-op, so the route is idempotent by construction. This is the documented
    carve-out from ADR-0003's blanket rule for state-creating mutations; the
    Web client still sends a key for consistency with every other mutation.
    """
    return _change_star(request, symbol, False)
