"""Symbol universe domain models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SymbolInfo(BaseModel):
    """One tradable contract in the universe."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    tradifi: bool = False


class SymbolUniverse(BaseModel):
    """Raw universe as loaded from a fixture or exchange adapter."""

    model_config = ConfigDict(frozen=True)

    source: str
    as_of: str
    symbols: tuple[SymbolInfo, ...]


class SymbolUniverseSummary(BaseModel):
    """Stable read model shared by the API and the CLI."""

    model_config = ConfigDict(frozen=True)

    source: str
    as_of: str
    count: int
    tradifi_count: int
    symbols: tuple[SymbolInfo, ...]


def summarize_universe(universe: SymbolUniverse) -> SymbolUniverseSummary:
    """Summarize a universe without network or storage access."""
    return SymbolUniverseSummary(
        source=universe.source,
        as_of=universe.as_of,
        count=len(universe.symbols),
        tradifi_count=sum(1 for item in universe.symbols if item.tradifi),
        symbols=universe.symbols,
    )
