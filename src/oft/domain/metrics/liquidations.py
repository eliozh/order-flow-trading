"""Live-only liquidation accumulator.

The USD-M ``forceOrder`` feed is live-only: only the largest liquidation per
symbol per second is pushed, and no historical backfill exists. Coverage is
therefore labelled since deployment (``coverage_start`` is the recorder's
deployment time), and both limitations are always emitted.

Side mapping: a forced ``SELL`` closes a long position (long liquidated); a
forced ``BUY`` closes a short position (short liquidated).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Literal, cast

from oft.domain.metrics.types import decimal_str
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort

MONEY_QUANTUM = Decimal("0.00000001")
DEFAULT_BUCKET_SECONDS = 60
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
LiquidationSide = Literal["BUY", "SELL"]
Coverage = Literal["SINCE_DEPLOYMENT", "NONE", "PARTIAL"]

LIMITATIONS = (
    "The liquidation WebSocket feed pushes only the largest liquidation per "
    "symbol per second; totals undercount.",
    "No historical USD-M liquidation backfill exists; coverage starts at "
    "deployment (since deployment).",
)


def _epoch_millis(value: datetime) -> int:
    """Integer epoch milliseconds; never goes through binary float."""
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class LiquidationEvent:
    """One normalized public ``forceOrder`` event."""

    received_at: datetime
    trade_time: datetime
    symbol: str
    side: LiquidationSide
    price: Decimal
    average_price: Decimal
    quantity: Decimal
    order_type: str = "LIMIT"
    time_in_force: str = "IOC"
    order_status: str = "FILLED"
    last_fill_quantity: Decimal | None = None
    source_seq: int | None = None

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.trade_time.tzinfo is None:
            raise ValueError("liquidation timestamps must be timezone-aware")
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("liquidation symbol is invalid")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("liquidation side must be BUY or SELL")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("liquidation price must be a positive finite decimal")
        if not self.average_price.is_finite() or self.average_price < 0:
            raise ValueError("liquidation average_price must be a finite decimal")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("liquidation quantity must be a positive finite decimal")
        if self.last_fill_quantity is not None and (
            not self.last_fill_quantity.is_finite() or self.last_fill_quantity <= 0
        ):
            raise ValueError("liquidation last_fill_quantity must be positive")
        if self.source_seq is not None and (
            isinstance(self.source_seq, bool) or not isinstance(self.source_seq, int)
        ):
            raise ValueError("liquidation source_seq must be an integer")

    @property
    def notional(self) -> Decimal:
        """USD notional; average price when present, else the order price."""
        price = self.average_price if self.average_price > 0 else self.price
        return _quantize_money(price * self.quantity)


@dataclass(frozen=True)
class LiquidationBucket:
    """One fixed time bucket of liquidation notional."""

    bucket_start: datetime
    bucket_end: datetime
    long_notional: Decimal
    short_notional: Decimal
    count: int

    def to_contract(self) -> dict[str, object]:
        return {
            "bucket_start": utc_timestamp(self.bucket_start),
            "bucket_end": utc_timestamp(self.bucket_end),
            "long_notional": decimal_str(self.long_notional),
            "short_notional": decimal_str(self.short_notional),
            "count": self.count,
        }


@dataclass(frozen=True)
class LiquidationResult:
    """A liquidation section explicitly labelled since deployment."""

    coverage: Coverage
    coverage_start: datetime | None
    coverage_end: datetime | None
    total_long_notional: Decimal
    total_short_notional: Decimal
    count: int
    buckets: tuple[LiquidationBucket, ...]
    limitations: tuple[str, ...] = LIMITATIONS

    def to_contract(self) -> dict[str, object]:
        return {
            "coverage": self.coverage,
            "coverage_start": (
                None if self.coverage_start is None else utc_timestamp(self.coverage_start)
            ),
            "coverage_end": (
                None if self.coverage_end is None else utc_timestamp(self.coverage_end)
            ),
            "total_long_notional": decimal_str(self.total_long_notional),
            "total_short_notional": decimal_str(self.total_short_notional),
            "count": self.count,
            "buckets": [bucket.to_contract() for bucket in self.buckets],
            "limitations": list(self.limitations),
        }


def liquidation_sort_key(event: LiquidationEvent) -> tuple[object, ...]:
    """Canonical event order shared by hashing and part publication."""
    return (
        event.received_at,
        event.trade_time,
        event.source_seq if event.source_seq is not None else -1,
        event.symbol,
        event.side,
        event.average_price,
        event.price,
        event.quantity,
        event.last_fill_quantity if event.last_fill_quantity is not None else Decimal("0"),
    )


def _event_key(event: LiquidationEvent) -> tuple[object, ...]:
    """Deduplication identity: source sequence when present, else full event."""
    if event.source_seq is not None:
        return ("seq", event.source_seq)
    return (
        "event",
        event.received_at,
        event.trade_time,
        event.symbol,
        event.side,
        event.price,
        event.average_price,
        event.quantity,
        event.last_fill_quantity,
    )


def _dedupe_events(events: Sequence[LiquidationEvent]) -> list[LiquidationEvent]:
    seen: set[tuple[object, ...]] = set()
    unique: list[LiquidationEvent] = []
    for event in events:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def compute_liquidations(
    events: Sequence[LiquidationEvent],
    *,
    symbol: str,
    coverage_start: datetime | None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
) -> LiquidationResult:
    """Aggregate long/short notional and buckets while keeping coverage honest.

    ``coverage_start=None`` means the recorder has not started: coverage is
    ``NONE`` and totals are zero. Once recording starts, coverage is
    ``SINCE_DEPLOYMENT`` even when no event has arrived yet.
    """
    if isinstance(bucket_seconds, bool) or not isinstance(bucket_seconds, int):
        raise ValueError("bucket_seconds must be a positive integer")
    if bucket_seconds < 1:
        raise ValueError("bucket_seconds must be a positive integer")
    if (window_start is None) != (window_end is None):
        raise ValueError("window_start and window_end must be supplied together")
    for bound in (window_start, window_end):
        if bound is not None and bound.tzinfo is None:
            raise ValueError("window bounds must be timezone-aware")
    if window_start is not None and window_end is not None and window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if coverage_start is not None and coverage_start.tzinfo is None:
        raise ValueError("coverage_start must be timezone-aware")

    events = tuple(_dedupe_events(events))

    if coverage_start is None:
        if events:
            raise ValueError("coverage_start is required when liquidations exist")
        return LiquidationResult(
            coverage="NONE",
            coverage_start=None,
            coverage_end=None,
            total_long_notional=Decimal("0"),
            total_short_notional=Decimal("0"),
            count=0,
            buckets=(),
        )

    selected: list[LiquidationEvent] = []
    for event in events:
        if event.symbol != symbol:
            raise ValueError(f"liquidation symbol {event.symbol!r} does not match {symbol!r}")
        if event.received_at < coverage_start:
            raise ValueError("liquidation event predates coverage start (since deployment)")
        if window_start is not None and event.received_at < window_start:
            continue
        if window_end is not None and event.received_at >= window_end:
            continue
        selected.append(event)

    total_long = Decimal("0")
    total_short = Decimal("0")
    bucket_totals: dict[int, list[object]] = {}
    for event in selected:
        notional = event.notional
        index = _epoch_millis(event.received_at) // (bucket_seconds * 1_000)
        if event.side == "SELL":
            total_long += notional
            bucket = bucket_totals.setdefault(index, [Decimal("0"), Decimal("0"), 0])
            bucket[0] = cast("Decimal", bucket[0]) + notional
        else:
            total_short += notional
            bucket = bucket_totals.setdefault(index, [Decimal("0"), Decimal("0"), 0])
            bucket[1] = cast("Decimal", bucket[1]) + notional
        bucket[2] = cast("int", bucket[2]) + 1

    buckets: list[LiquidationBucket] = []
    for index in sorted(bucket_totals):
        long_notional, short_notional, count = bucket_totals[index]
        bucket_start = _EPOCH + timedelta(seconds=index * bucket_seconds)
        buckets.append(
            LiquidationBucket(
                bucket_start=bucket_start,
                bucket_end=bucket_start + timedelta(seconds=bucket_seconds),
                long_notional=cast("Decimal", long_notional),
                short_notional=cast("Decimal", short_notional),
                count=cast("int", count),
            )
        )

    return LiquidationResult(
        coverage="SINCE_DEPLOYMENT",
        coverage_start=coverage_start,
        coverage_end=max((event.received_at for event in selected), default=None),
        total_long_notional=_quantize_money(total_long),
        total_short_notional=_quantize_money(total_short),
        count=len(selected),
        buckets=tuple(buckets),
    )


def liquidation_input_hash(events: Sequence[LiquidationEvent], symbol: str) -> str:
    """Deterministic hash of the recorded liquidation row set."""
    digest = hashlib.sha256()
    digest.update(f"{symbol}\n".encode())
    ordered = sorted(events, key=liquidation_sort_key)
    for event in ordered:
        last_fill = (
            decimal_str(event.last_fill_quantity) if event.last_fill_quantity is not None else ""
        )
        digest.update(
            (
                f"{utc_timestamp(event.received_at)}|{utc_timestamp(event.trade_time)}|"
                f"{event.side}|{decimal_str(event.price)}|"
                f"{decimal_str(event.average_price)}|{decimal_str(event.quantity)}|"
                f"{last_fill}|{event.source_seq}\n"
            ).encode()
        )
    return digest.hexdigest()


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an exact decimal string")
    try:
        parsed = Decimal(value)
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"{field} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _parse_source_seq(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("source_seq must be an integer")
    return value


def load_liquidation_events(path: Path) -> tuple[LiquidationEvent, ...]:
    """Read normalized recorded/live-shaped liquidation events from JSONL.

    These are public ``forceOrder``-shaped rows. They are not historical USD-M
    liquidation data, which does not exist. Decimal fields must be JSON
    strings so binary floats never enter the metric path.
    """
    if not path.is_file():
        raise ValueError(f"liquidation fixture not found: {path}")
    events: list[LiquidationEvent] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read liquidation fixture {path.name}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("liquidation row must be a JSON object")
            required = {
                "received_at",
                "trade_time",
                "symbol",
                "side",
                "price",
                "average_price",
                "quantity",
            }
            missing = required - set(row)
            if missing:
                raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
            last_fill = row.get("last_fill_quantity")
            source_seq = row.get("source_seq")
            events.append(
                LiquidationEvent(
                    received_at=_parse_timestamp(row["received_at"], "received_at"),
                    trade_time=_parse_timestamp(row["trade_time"], "trade_time"),
                    symbol=str(row["symbol"]),
                    side=cast("LiquidationSide", str(row["side"])),
                    price=_parse_decimal(row["price"], "price"),
                    average_price=_parse_decimal(row["average_price"], "average_price"),
                    quantity=_parse_decimal(row["quantity"], "quantity"),
                    order_type=str(row.get("order_type", "LIMIT")),
                    time_in_force=str(row.get("time_in_force", "IOC")),
                    order_status=str(row.get("order_status", "FILLED")),
                    last_fill_quantity=(
                        None
                        if last_fill is None
                        else _parse_decimal(last_fill, "last_fill_quantity")
                    ),
                    source_seq=_parse_source_seq(source_seq),
                )
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid liquidation fixture row {number}: {exc}") from exc
    return tuple(_dedupe_events(events))


class LiquidationRecorder:
    """Accumulates live liquidation events and labels coverage since deployment."""

    def __init__(
        self,
        *,
        clock: ClockPort,
        symbol: str = "BTCUSDT",
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> None:
        if isinstance(bucket_seconds, bool) or not isinstance(bucket_seconds, int):
            raise ValueError("bucket_seconds must be a positive integer")
        if bucket_seconds < 1:
            raise ValueError("bucket_seconds must be a positive integer")
        self._clock = clock
        self._symbol = symbol
        self._bucket_seconds = bucket_seconds
        self._coverage_start: datetime | None = None
        self._events: list[LiquidationEvent] = []
        self._seen: set[object] = set()

    @property
    def coverage_start(self) -> datetime | None:
        return self._coverage_start

    def start(self) -> datetime:
        """Record deployment time once; idempotent."""
        if self._coverage_start is None:
            self._coverage_start = self._clock.now()
        return self._coverage_start

    def record(self, event: LiquidationEvent) -> bool:
        """Record one event; duplicate identities are ignored."""
        key = _event_key(event)
        if key in self._seen:
            return False
        self.start()
        self._seen.add(key)
        self._events.append(event)
        return True

    def events(self) -> tuple[LiquidationEvent, ...]:
        return tuple(self._events)

    def summary(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> LiquidationResult:
        return compute_liquidations(
            self._events,
            symbol=self._symbol,
            coverage_start=self._coverage_start,
            window_start=window_start,
            window_end=window_end,
            bucket_seconds=self._bucket_seconds,
        )

    @classmethod
    def from_jsonl(
        cls,
        path: Path,
        *,
        clock: ClockPort,
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> LiquidationRecorder:
        """Build a recorder from recorded/live-shaped events, starting coverage."""
        events = load_liquidation_events(path)
        symbol = events[0].symbol if events else "BTCUSDT"
        recorder = cls(clock=clock, symbol=symbol, bucket_seconds=bucket_seconds)
        recorder.start()
        for event in events:
            recorder.record(event)
        return recorder
