"""Read-only symbol registry routes (ADR-0003 symbols group)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.domain.symbols import build_symbol_detail
from oft.domain.time import utc_timestamp

router = APIRouter()

_MAX_SEARCH_LENGTH = 64
_MAX_CONTRACT_TYPE_LENGTH = 32
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 2000


def _registry(request: Request) -> Any:
    return request.app.state.container.symbol_registry


@router.get("/api/v1/symbols")
def list_symbols(
    request: Request,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=_MAX_SEARCH_LENGTH,
            description="Symbol or base-asset search.",
        ),
    ] = None,
    group: Annotated[
        Literal["crypto", "tradfi"] | None,
        Query(description="Crypto or TradFi grouping."),
    ] = None,
    contract_type: Annotated[
        str | None,
        Query(max_length=_MAX_CONTRACT_TYPE_LENGTH, description="exchangeInfo contractType."),
    ] = None,
    starred: Annotated[bool | None, Query(description="Filter to starred symbols.")] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> dict[str, Any]:
    """List registry contracts with search, filter, and envelope pagination."""
    registry = _registry(request)
    tradifi = None if group is None else group == "tradfi"
    try:
        items = registry.list_symbols(
            query=q,
            tradifi=tradifi,
            contract_type=contract_type,
            starred=starred,
            limit=limit,
            offset=offset,
        )
        total = registry.count_symbols(
            query=q, tradifi=tradifi, contract_type=contract_type, starred=starred
        )
    except OverflowError as exc:
        raise ApiError(
            "VALIDATION_FAILED", "Pagination offset is outside the supported range."
        ) from exc
    returned = len(items)
    has_next = offset + returned < total
    pagination = {
        "limit": limit,
        "cursor": None,
        "has_next": has_next,
        "next_cursor": str(offset + limit) if has_next else None,
        "total": total,
    }
    return success_envelope(
        {
            "items": [item.model_dump(mode="json") for item in items],
            "total": total,
            "query": q,
            "group": group,
            "contract_type": contract_type,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if total > 0 else "MISSING",
        pagination=pagination,
    )


@router.get("/api/v1/symbols/{symbol}")
def get_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Return contract specs plus data-coverage placeholders for one symbol."""
    contract = _registry(request).get_symbol(symbol)
    if contract is None:
        raise ApiError("NOT_FOUND", f"Symbol {symbol} does not exist.")
    detail = build_symbol_detail(contract)
    return success_envelope(
        detail.model_dump(mode="json"),
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )


class StarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starred: bool


@router.patch("/api/v1/symbols/{symbol}/star")
def set_star(symbol: str, payload: StarRequest, request: Request) -> dict[str, Any]:
    """Star or unstar one registry symbol (idempotent)."""
    registry = _registry(request)
    starred_at = utc_timestamp(request.app.state.container.clock.now())
    updated = registry.set_starred(symbol, payload.starred, starred_at=starred_at)
    if not updated:
        raise ApiError("NOT_FOUND", f"Symbol {symbol} does not exist.")
    resolved = symbol.strip().upper()
    return success_envelope(
        {
            "symbol": resolved,
            "starred": payload.starred,
            "starred_at": starred_at if payload.starred else None,
        },
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE",
    )
