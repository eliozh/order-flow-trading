"""Canonical market-data events shared by every ingestion path."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AggTrade(BaseModel):
    """One normalized Binance ``aggTrade`` record.

    ``is_buyer_maker`` is kept verbatim from the source; the taker side is
    derived in :mod:`oft.domain.metrics.delta`, never stored twice.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=r"^[A-Z0-9]{2,24}$")
    agg_trade_id: int = Field(ge=0)
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    first_trade_id: int = Field(ge=0)
    last_trade_id: int = Field(ge=0)
    transact_time: datetime
    is_buyer_maker: bool

    @field_validator("transact_time")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("transact_time must be timezone-aware")
        return value
