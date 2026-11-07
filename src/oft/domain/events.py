"""Canonical market-data events shared by every ingestion path.

Live WebSocket payloads are untrusted input. Every field is validated at this
boundary: enums and ranges are checked, text is length-capped, and control or
zero-width characters are stripped before the value is stored or logged.
Invalid records raise :class:`EventValidationError`; the supervisor counts them
and never crashes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_MESSAGE_BYTES = 64 * 1024
MAX_TEXT_FIELD_LENGTH = 64
MAX_DECIMAL_TEXT_LENGTH = 64
MAX_INTEGER = 2**63 - 1
MIN_EVENT_YEAR = 2017
MAX_EVENT_YEAR = 2100

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL_AND_INVISIBLE = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")
_SYMBOL_PATTERN = r"^[A-Z0-9]{2,24}$"
_INTERVAL_PATTERN = r"^[0-9]{1,3}[mhdw]$"


class EventValidationError(Exception):
    """A hostile or malformed stream record; carries a stable ``reason``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def strip_control_characters(value: str) -> str:
    """Remove ANSI escapes, control, zero-width, bidi, and BOM characters."""
    return _CONTROL_AND_INVISIBLE.sub("", _ANSI_ESCAPE.sub("", value))


def _text(
    value: object,
    field: str,
    *,
    max_length: int = MAX_TEXT_FIELD_LENGTH,
    upper: bool = False,
) -> str:
    if not isinstance(value, str):
        raise EventValidationError("INVALID_TYPE", f"{field} must be a string")
    cleaned = strip_control_characters(value).strip()
    if not cleaned:
        raise EventValidationError("INVALID_TEXT", f"{field} is empty after sanitization")
    if len(cleaned) > max_length:
        raise EventValidationError("INVALID_TEXT", f"{field} exceeds {max_length} characters")
    return cleaned.upper() if upper else cleaned


def _int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventValidationError("INVALID_TYPE", f"{field} must be an integer")
    if value < minimum:
        raise EventValidationError("INVALID_RANGE", f"{field} must be >= {minimum}")
    if value > MAX_INTEGER:
        raise EventValidationError("INVALID_RANGE", f"{field} exceeds the int64 bound")
    return value


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise EventValidationError("INVALID_TYPE", f"{field} must be a decimal")
    if not isinstance(value, (str, int, Decimal)):
        raise EventValidationError("INVALID_TYPE", f"{field} must be a decimal string")
    if len(str(value)) > MAX_DECIMAL_TEXT_LENGTH:
        raise EventValidationError("INVALID_TEXT", f"{field} is too long")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise EventValidationError("INVALID_TYPE", f"{field} is not a decimal") from exc
    if not parsed.is_finite():
        raise EventValidationError("INVALID_RANGE", f"{field} must be finite")
    if positive and parsed <= 0:
        raise EventValidationError("INVALID_RANGE", f"{field} must be positive")
    if non_negative and parsed < 0:
        raise EventValidationError("INVALID_RANGE", f"{field} must be non-negative")
    return parsed


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise EventValidationError("INVALID_TYPE", f"{field} must be a boolean")
    return value


def _millis(value: object, field: str) -> datetime:
    milliseconds = _int(value, field)
    try:
        instant = datetime.fromtimestamp(milliseconds // 1000, tz=UTC) + timedelta(
            milliseconds=milliseconds % 1000
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise EventValidationError("INVALID_RANGE", f"{field} is not a valid timestamp") from exc
    if not (MIN_EVENT_YEAR <= instant.year <= MAX_EVENT_YEAR):
        raise EventValidationError(
            "INVALID_RANGE",
            f"{field} must be between {MIN_EVENT_YEAR} and {MAX_EVENT_YEAR}",
        )
    return instant


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _model_error(exc: ValidationError) -> EventValidationError:
    # Never echo the offending value: it may contain hostile log content.
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or "payload"
    error_type = str(first.get("type", "value_error"))
    reason = "INVALID_ENUM" if "literal" in error_type or "enum" in error_type else "INVALID_FIELD"
    return EventValidationError(reason, f"field {field} failed validation")


class AggTrade(BaseModel):
    """One normalized Binance ``aggTrade`` record.

    ``is_buyer_maker`` is kept verbatim from the source; the taker side is
    derived in :mod:`oft.domain.metrics.delta`, never stored twice.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=_SYMBOL_PATTERN)
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
        return _require_aware(value, "transact_time")


class KlineEvent(BaseModel):
    """One normalized ``kline`` stream event (1m bars for this slice)."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    event_time: datetime
    open_time: datetime
    close_time: datetime
    interval: str = Field(pattern=_INTERVAL_PATTERN)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    quote_volume: Decimal = Field(ge=0)
    trade_count: int = Field(ge=0)
    taker_buy_volume: Decimal = Field(ge=0)
    taker_buy_quote_volume: Decimal = Field(ge=0)
    is_closed: bool

    @field_validator("event_time", "open_time", "close_time")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return _require_aware(value, "kline timestamp")

    @model_validator(mode="after")
    def _validate_ranges(self) -> KlineEvent:
        if self.close_time < self.open_time:
            raise ValueError("close_time must not precede open_time")
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open must lie within [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close must lie within [low, high]")
        return self


class MarkPriceEvent(BaseModel):
    """One normalized ``markPriceUpdate`` event."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    event_time: datetime
    mark_price: Decimal = Field(gt=0)
    index_price: Decimal = Field(gt=0)
    funding_rate: Decimal
    next_funding_time: datetime | None = None

    @field_validator("event_time", "next_funding_time")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, "mark price timestamp")


class ForceOrderEvent(BaseModel):
    """One normalized public ``forceOrder`` (market-wide liquidation) event."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    event_time: datetime
    trade_time: datetime
    side: Literal["BUY", "SELL"]
    order_type: str
    time_in_force: str
    price: Decimal = Field(gt=0)
    average_price: Decimal = Field(ge=0)
    quantity: Decimal = Field(gt=0)
    order_status: str
    last_fill_quantity: Decimal | None = None
    source_seq: int | None = None

    @field_validator("event_time", "trade_time")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return _require_aware(value, "forceOrder timestamp")

    def to_dict(self) -> dict[str, object]:
        return event_to_dict(self)


type NormalizedEvent = AggTrade | KlineEvent | MarkPriceEvent | ForceOrderEvent


def parse_stream_message(
    raw: str,
    *,
    max_bytes: int = MAX_MESSAGE_BYTES,
) -> tuple[str, dict[str, Any]]:
    """Bound, decode, and unwrap one combined or raw stream message.

    Returns ``(stream_name, payload)``. A raw message has an empty stream name;
    the caller derives it from the event itself.
    """
    if not isinstance(raw, str):
        raise EventValidationError("INVALID_TYPE", "stream message must be text")
    if len(raw.encode("utf-8")) > max_bytes:
        raise EventValidationError("OVERSIZED", f"stream message exceeds {max_bytes} bytes")
    try:
        document: Any = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise EventValidationError("MALFORMED_JSON", "stream message is not valid JSON") from exc
    if not isinstance(document, dict):
        raise EventValidationError("NOT_AN_OBJECT", "stream message must be a JSON object")
    if "stream" in document or "data" in document:
        stream = _text(document.get("stream"), "stream", max_length=128)
        data = document.get("data")
        if not isinstance(data, dict):
            raise EventValidationError("NOT_AN_OBJECT", "combined stream data must be an object")
        return stream, data
    return "", document


def normalize_event(payload: Mapping[str, Any]) -> NormalizedEvent:
    """Validate one untrusted event payload into a canonical event."""
    event_type = payload.get("e")
    if not isinstance(event_type, str):
        raise EventValidationError("MISSING_FIELD", "event type field e is required")
    event_type = strip_control_characters(event_type).strip()
    try:
        if event_type == "aggTrade":
            return _normalize_agg_trade(payload)
        if event_type == "kline":
            return _normalize_kline(payload)
        if event_type == "markPriceUpdate":
            return _normalize_mark_price(payload)
        if event_type == "forceOrder":
            return _normalize_force_order(payload)
    except ValidationError as exc:
        raise _model_error(exc) from exc
    raise EventValidationError("UNKNOWN_EVENT", "unsupported event type")


def _normalize_agg_trade(payload: Mapping[str, Any]) -> AggTrade:
    return AggTrade(
        symbol=_text(payload.get("s"), "s", max_length=24, upper=True),
        agg_trade_id=_int(payload.get("a"), "a"),
        price=_decimal(payload.get("p"), "p", positive=True),
        quantity=_decimal(payload.get("q"), "q", positive=True),
        first_trade_id=_int(payload.get("f"), "f"),
        last_trade_id=_int(payload.get("l"), "l"),
        transact_time=_millis(payload.get("T"), "T"),
        is_buyer_maker=_bool(payload.get("m"), "m"),
    )


def _normalize_kline(payload: Mapping[str, Any]) -> KlineEvent:
    kline = payload.get("k")
    if not isinstance(kline, dict):
        raise EventValidationError("MISSING_FIELD", "kline field k is required")
    return KlineEvent(
        symbol=_text(payload.get("s"), "s", max_length=24, upper=True),
        event_time=_millis(payload.get("E"), "E"),
        open_time=_millis(kline.get("t"), "k.t"),
        close_time=_millis(kline.get("T"), "k.T"),
        interval=_text(kline.get("i"), "k.i", max_length=8),
        open=_decimal(kline.get("o"), "k.o", positive=True),
        high=_decimal(kline.get("h"), "k.h", positive=True),
        low=_decimal(kline.get("l"), "k.l", positive=True),
        close=_decimal(kline.get("c"), "k.c", positive=True),
        volume=_decimal(kline.get("v"), "k.v", non_negative=True),
        quote_volume=_decimal(kline.get("q"), "k.q", non_negative=True),
        trade_count=_int(kline.get("n"), "k.n"),
        taker_buy_volume=_decimal(kline.get("V"), "k.V", non_negative=True),
        taker_buy_quote_volume=_decimal(kline.get("Q"), "k.Q", non_negative=True),
        is_closed=_bool(kline.get("x"), "k.x"),
    )


def _normalize_mark_price(payload: Mapping[str, Any]) -> MarkPriceEvent:
    next_funding = payload.get("T")
    return MarkPriceEvent(
        symbol=_text(payload.get("s"), "s", max_length=24, upper=True),
        event_time=_millis(payload.get("E"), "E"),
        mark_price=_decimal(payload.get("p"), "p", positive=True),
        index_price=_decimal(payload.get("i"), "i", positive=True),
        funding_rate=_decimal(payload.get("r"), "r"),
        next_funding_time=(_millis(next_funding, "T") if next_funding is not None else None),
    )


def _normalize_force_order(payload: Mapping[str, Any]) -> ForceOrderEvent:
    order = payload.get("o")
    if not isinstance(order, dict):
        raise EventValidationError("MISSING_FIELD", "forceOrder field o is required")
    side = _text(order.get("S"), "o.S", max_length=8, upper=True)
    if side not in ("BUY", "SELL"):
        raise EventValidationError("INVALID_ENUM", "o.S must be BUY or SELL")
    last_fill = order.get("l")
    symbol = payload.get("s") if payload.get("s") is not None else order.get("s")
    return ForceOrderEvent(
        symbol=_text(symbol, "s", max_length=24, upper=True),
        event_time=_millis(payload.get("E"), "E"),
        trade_time=_millis(order.get("T"), "o.T"),
        side=side,
        order_type=_text(order.get("o"), "o.o", max_length=16, upper=True),
        time_in_force=_text(order.get("f"), "o.f", max_length=8, upper=True),
        price=_decimal(order.get("p"), "o.p", positive=True),
        average_price=_decimal(order.get("ap"), "o.ap", non_negative=True),
        quantity=_decimal(order.get("q"), "o.q", positive=True),
        order_status=_text(order.get("X"), "o.X", max_length=16, upper=True),
        last_fill_quantity=(
            _decimal(last_fill, "o.l", non_negative=True) if last_fill is not None else None
        ),
        source_seq=None,
    )


_EVENT_KIND: dict[type[BaseModel], str] = {
    AggTrade: "aggTrade",
    KlineEvent: "kline",
    MarkPriceEvent: "markPriceUpdate",
    ForceOrderEvent: "forceOrder",
}


def event_to_dict(event: NormalizedEvent) -> dict[str, Any]:
    """Serialize a canonical event for checkpoints, WAL, and quarantine."""
    kind = _EVENT_KIND[type(event)]
    document = event.model_dump(mode="json")
    document["kind"] = kind
    return document


def event_from_dict(document: Mapping[str, Any]) -> NormalizedEvent:
    """Rehydrate a canonical event written by :func:`event_to_dict`."""
    kind = document.get("kind")
    models: dict[str, type[BaseModel]] = {
        "aggTrade": AggTrade,
        "kline": KlineEvent,
        "markPriceUpdate": MarkPriceEvent,
        "forceOrder": ForceOrderEvent,
    }
    model = models.get(str(kind))
    if model is None:
        raise EventValidationError("UNKNOWN_EVENT", "unknown stored event kind")
    payload = {key: value for key, value in document.items() if key != "kind"}
    try:
        return model.model_validate(payload)  # type: ignore[return-value]
    except ValidationError as exc:
        raise _model_error(exc) from exc


def event_stream_name(event: NormalizedEvent) -> str:
    """Derive the Binance stream name for a raw (non-combined) message."""
    symbol = event.symbol.lower()
    if isinstance(event, AggTrade):
        return f"{symbol}@aggTrade"
    if isinstance(event, KlineEvent):
        return f"{symbol}@kline_{event.interval}"
    if isinstance(event, MarkPriceEvent):
        return f"{symbol}@markPrice@1s"
    return f"{symbol}@forceOrder"
