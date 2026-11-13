"""Read the closed-trade journal (minimal list projection, full show)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Request

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ApiError
from oft.api.schemas.journal import Envelope, TradeLogListData
from oft.application.payloads import decode_payload_json

router = APIRouter()


def trade_log_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The minimal journal projection shared by the list route and the CLI."""
    return {
        "trade_id": payload.get("trade_id"),
        "position_id": payload.get("position_id"),
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "opened_at": payload.get("opened_at"),
        "closed_at": payload.get("closed_at"),
        "gross_pnl_usd": payload.get("gross_pnl_usd"),
        "fees_usd": payload.get("fees_usd"),
        "funding_usd": payload.get("funding_usd"),
        "net_pnl_usd": payload.get("net_pnl_usd"),
        "return_pct": payload.get("return_pct"),
        "r_multiple": payload.get("r_multiple"),
        "mae_usd": payload.get("mae_usd"),
        "mfe_usd": payload.get("mfe_usd"),
        "outcome_tags": payload.get("outcome_tags") or [],
        "data_quality": payload.get("data_quality"),
    }


@router.get("/api/v1/journal", response_model=Envelope[TradeLogListData])
def list_journal(
    request: Request,
    symbol: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    store = request.app.state.container.store
    resolved_symbol = symbol.upper() if symbol else None
    records = store.list_trade_logs(symbol=resolved_symbol, limit=limit, offset=offset)
    items: list[dict[str, Any]] = []
    for record in records:
        payload = decode_payload_json(record.payload_json, subject=f"trade log {record.trade_id}")
        if payload is not None:
            items.append(trade_log_summary(payload))
    total = store.count_trade_logs(symbol=resolved_symbol)
    has_next = offset + len(items) < total
    return success_envelope(
        {"items": items, "total": total},
        request_id=new_request_id(),
        provenance="CALC",
        data_quality="COMPLETE" if items else "MISSING",
        pagination={
            "limit": limit,
            "cursor": str(offset),
            "has_next": has_next,
            "next_cursor": str(offset + limit) if has_next else None,
            "total": total,
        },
    )


@router.get("/api/v1/journal/{trade_id}", response_model=Envelope[dict[str, Any]])
def read_journal_trade(
    request: Request,
    trade_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> dict[str, Any]:
    store = request.app.state.container.store
    record = store.get_trade_log(trade_id)
    if record is None:
        raise ApiError("NOT_FOUND", f"Trade {trade_id} does not exist.")
    payload = decode_payload_json(record.payload_json, subject=f"trade log {trade_id}")
    if payload is None:
        raise ApiError("DATA_UNAVAILABLE", f"Trade {trade_id} payload is unreadable.")
    return success_envelope(
        payload,
        request_id=new_request_id(),
        generated_at=str(payload.get("closed_at") or ""),
        provenance="CALC",
        data_quality=str((payload.get("data_quality") or {}).get("status", "COMPLETE")),
    )


__all__ = ["router", "trade_log_summary"]
