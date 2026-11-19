"""Market-data ports used by the walking skeleton and live ingestion."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from oft.domain.events import AggTrade, NormalizedEvent
from oft.domain.symbols import SymbolUniverse


class MarketDataPort(Protocol):
    """Read access to the symbol universe and committed fixtures."""

    def load_universe(self) -> SymbolUniverse: ...

    def load_agg_trades(self, fixture_path: Path | str, symbol: str) -> tuple[AggTrade, ...]: ...


class WebSocketTransport(Protocol):
    """Blocking transport abstraction so tests never open a socket."""

    def connect(self) -> None: ...

    def send(self, text: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


class MarketDataStreamPort(Protocol):
    """Live stream client used by the ingestion supervisor."""

    @property
    def counters(self) -> dict[str, int]: ...

    def connect(self, streams: Sequence[str], *, url: str | None = None) -> None: ...

    def subscribe(self, streams: Sequence[str], *, request_id: int = 1) -> object: ...

    def receive_event(self, timeout: float | None = None) -> tuple[str, dict[str, object]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class AggTradeRecovery:
    """Outcome of a REST gap-recovery window (last 48 h for ``aggTrades``)."""

    trades: tuple[AggTrade, ...]
    requests: int
    weight_used: int | None
    complete: bool
    reason: str | None
    start: datetime
    end: datetime


class AggTradeRecoveryPort(Protocol):
    """Recover ``aggTrades`` missed while the socket was disconnected."""

    def recover_agg_trades(
        self,
        symbol: str,
        *,
        last_event_time: datetime | None,
        now: datetime,
        stop: Callable[[], bool] | None = None,
    ) -> AggTradeRecovery:
        """Recover a bounded window; ``stop`` is a cooperative shutdown hook.

        Implementations should check ``stop`` between network calls and return
        a partial result (``complete=False``) once it returns ``True`` so a
        daemon shutdown does not wait for the full 48 h window scan.
        """
        ...


class EventSinkPort(Protocol):
    """Persist normalized events in bounded batches."""

    def add(self, event: NormalizedEvent) -> None: ...

    def flush(self) -> tuple[object, ...]: ...
