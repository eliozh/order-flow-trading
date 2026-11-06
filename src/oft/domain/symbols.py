"""Symbol universe domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field

#: exchangeInfo contract types this registry accepts.
ACCEPTED_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
TRADIFI_CONTRACT_TYPE = "TRADIFI_PERPETUAL"


class SymbolInfo(BaseModel):
    """One tradable contract in the universe."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    tradifi: bool = False


class SymbolContract(BaseModel):
    """Immutable contract specification parsed from ``exchangeInfo``.

    Quantities and prices are exact decimals and serialize to JSON strings, so
    no float ever reaches the API, the CLI, or SQLite.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    underlying_type: str | None
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    onboard_date: int
    tradifi: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def listed_date(self) -> str:
        """Listing date (UTC) derived from Binance's millisecond onboardDate."""
        return datetime.fromtimestamp(self.onboard_date // 1000, tz=UTC).date().isoformat()


class SymbolCoverage(BaseModel):
    """Data-coverage placeholders for this slice (no backfill yet)."""

    model_config = ConfigDict(frozen=True)

    status: str = "UNKNOWN"
    coverage_pct: float | None = None
    history_start: str
    book_ticker: str = "N/A"
    liquidation_history: str = "SINCE_DEPLOYMENT"
    latest_metric_at: str | None = None
    notes: tuple[str, ...] = (
        "Coverage is measured after backfill; no backfill runs in this slice.",
        "Liquidation history exists only from deployment onward.",
    )


class SymbolDetail(BaseModel):
    """Inspector read model: contract spec plus coverage placeholders."""

    model_config = ConfigDict(frozen=True)

    contract: SymbolContract
    coverage: SymbolCoverage


def build_symbol_detail(contract: SymbolContract) -> SymbolDetail:
    """Build the read-only inspector payload for one contract."""
    return SymbolDetail(
        contract=contract,
        coverage=SymbolCoverage(history_start=contract.listed_date),
    )


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
