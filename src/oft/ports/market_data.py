"""Market-data port used by the walking skeleton."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from oft.domain.events import AggTrade
from oft.domain.symbols import SymbolUniverse


class MarketDataPort(Protocol):
    """Read access to the symbol universe and committed fixtures."""

    def load_universe(self) -> SymbolUniverse: ...

    def load_agg_trades(self, fixture_path: Path | str, symbol: str) -> tuple[AggTrade, ...]: ...
