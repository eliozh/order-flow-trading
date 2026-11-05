"""Market-data port used by the walking skeleton."""

from __future__ import annotations

from typing import Protocol

from oft.domain.symbols import SymbolUniverse


class MarketDataPort(Protocol):
    """Read access to the symbol universe."""

    def load_universe(self) -> SymbolUniverse: ...
