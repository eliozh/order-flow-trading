"""Bounded, event-time ordered replay sources.

A replay source never reads the live daemon store or a socket. It reads a
committed fixture (or an in-memory event tuple), rejects events outside the
declared half-open window, rejects duplicate ids, and emits rows in
``(transact_time, agg_trade_id)`` order so the harness can advance its clock
with no lookahead.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from oft.domain.events import AggTrade

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
_REQUIRED_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)


class EventSourceError(ValueError):
    """The replay fixture is malformed, unordered, or outside its window."""


@dataclass(frozen=True)
class ReplayWindow:
    """The half-open event-time window a replay covers."""

    symbol: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("replay symbol must match ^[A-Z0-9]{2,24}$")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("replay window bounds must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("replay window_end must be after window_start")


class ReplayEventSource(Protocol):
    """Anything that can emit a validated event-time ordered replay stream."""

    @property
    def window(self) -> ReplayWindow: ...

    @property
    def reference(self) -> str: ...

    def iter_events(self) -> Iterator[AggTrade]: ...


def _parse_ms(raw: str) -> datetime:
    milliseconds = int(raw)
    try:
        return datetime.fromtimestamp(milliseconds // 1000, tz=UTC) + timedelta(
            milliseconds=milliseconds % 1000
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise EventSourceError(f"replay timestamp is out of range: {raw}") from exc


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise EventSourceError(f"invalid is_buyer_maker value {raw!r}; expected true or false")


def load_agg_trade_csv(path: Path, *, symbol: str) -> tuple[AggTrade, ...]:
    """Read a Binance-shaped aggTrades CSV into canonical events."""
    if not path.is_file():
        raise EventSourceError(f"replay fixture not found: {path.name}")
    trades: list[AggTrade] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or ())
            missing = [name for name in _REQUIRED_COLUMNS if name not in fieldnames]
            if missing:
                raise EventSourceError(
                    f"replay fixture {path.name} is missing columns: {', '.join(missing)}"
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    trades.append(
                        AggTrade(
                            symbol=symbol,
                            agg_trade_id=int(row["agg_trade_id"]),
                            price=Decimal(row["price"]),
                            quantity=Decimal(row["quantity"]),
                            first_trade_id=int(row["first_trade_id"]),
                            last_trade_id=int(row["last_trade_id"]),
                            transact_time=_parse_ms(row["transact_time"]),
                            is_buyer_maker=_parse_bool(row["is_buyer_maker"]),
                        )
                    )
                except (KeyError, ValueError, InvalidOperation) as exc:
                    raise EventSourceError(
                        f"invalid aggTrade row at line {line_number}: {exc}"
                    ) from exc
    except OSError as exc:
        raise EventSourceError(f"cannot read replay fixture {path.name}: {exc}") from exc
    return tuple(trades)


def _validated_events(events: Sequence[AggTrade], window: ReplayWindow) -> tuple[AggTrade, ...]:
    """Sort, then reject duplicates or rows outside the half-open window."""
    for trade in events:
        if trade.symbol != window.symbol:
            raise EventSourceError(
                f"event symbol {trade.symbol!r} does not match {window.symbol!r}"
            )
        if trade.transact_time < window.start or trade.transact_time >= window.end:
            raise EventSourceError(
                f"event {trade.agg_trade_id} at {trade.transact_time.isoformat()} "
                "is outside the replay window"
            )
    ordered = sorted(events, key=lambda trade: (trade.transact_time, trade.agg_trade_id))
    seen: set[int] = set()
    for trade in ordered:
        if trade.agg_trade_id in seen:
            raise EventSourceError(f"duplicate agg_trade_id {trade.agg_trade_id}")
        seen.add(trade.agg_trade_id)
    return tuple(ordered)


@dataclass(frozen=True)
class MemoryEventSource:
    """An in-memory event source used by tests and composed scenarios."""

    window: ReplayWindow
    events: tuple[AggTrade, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", _validated_events(self.events, self.window))

    @property
    def reference(self) -> str:
        return f"memory:{self.window.symbol}"

    def iter_events(self) -> Iterator[AggTrade]:
        yield from self.events


@dataclass(frozen=True)
class FixtureEventSource:
    """A fixture-backed source; reads only the given committed CSV file."""

    path: Path
    window: ReplayWindow

    @classmethod
    def from_csv(cls, path: Path | str, *, window: ReplayWindow) -> FixtureEventSource:
        return cls(path=Path(path), window=window)

    @property
    def reference(self) -> str:
        return self.path.name

    def _events(self) -> tuple[AggTrade, ...]:
        return _validated_events(
            load_agg_trade_csv(self.path, symbol=self.window.symbol),
            self.window,
        )

    def iter_events(self) -> Iterator[AggTrade]:
        yield from self._events()
