"""Read-only watchlist surface; star mutation belongs to a later milestone."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from oft.api.envelope import new_request_id, success_envelope

router = APIRouter()

_WATCHLIST_ROW_CAP = 2000


def _registry(request: Request) -> Any:
    return request.app.state.container.symbol_registry


@router.get("/api/v1/watchlist")
def watchlist(request: Request) -> dict[str, Any]:
    """Group registry contracts by crypto/TradFi; no star state exists yet."""
    registry = _registry(request)
    # One bounded read instead of two full-table reads.
    contracts = registry.list_symbols(limit=_WATCHLIST_ROW_CAP)
    crypto = [contract for contract in contracts if not contract.tradifi]
    tradfi = [contract for contract in contracts if contract.tradifi]
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
            "star_mutation": False,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if crypto or tradfi else "MISSING",
    )
