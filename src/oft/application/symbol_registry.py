"""exchangeInfo parsing and the SQLite-backed symbol registry."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from oft.clock import SystemClock
from oft.domain.symbols import (
    ACCEPTED_CONTRACT_TYPES,
    TRADIFI_CONTRACT_TYPE,
    SymbolContract,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import StorePort, SymbolUpsertResult

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
_TRADING = "TRADING"
_MIN_ONBOARD_MS = int(datetime(2017, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _max_onboard_ms() -> int:
    return int((datetime.now(UTC) + timedelta(days=1)).timestamp() * 1000)


class SymbolRegistryError(Exception):
    """The registry could not read or persist exchangeInfo."""

    code = "DATA_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ExchangeInfoParseError(SymbolRegistryError):
    """The exchangeInfo payload has the wrong top-level shape."""

    code = "SCHEMA_INVALID"


class _MalformedEntry(Exception):
    """One symbol entry is unusable; the registry skips it and continues."""


@dataclass(frozen=True)
class RejectedSymbol:
    """A malformed entry that was skipped without failing the ingest."""

    symbol: str | None
    reason: str


@dataclass(frozen=True)
class ParseResult:
    contracts: tuple[SymbolContract, ...]
    rejected: tuple[RejectedSymbol, ...]


@dataclass(frozen=True)
class SymbolIngestResult:
    inserted: int
    updated: int
    unchanged: int
    total: int
    rejected: tuple[RejectedSymbol, ...] = ()


class ExchangeInfoResponseLike(Protocol):
    """Structural type for an adapter response (keeps application adapter-free)."""

    payload: dict[str, Any]


class ExchangeInfoSource(Protocol):
    """Anything that can asynchronously return an exchangeInfo payload."""

    async def fetch_exchange_info(self) -> ExchangeInfoResponseLike: ...


def _required_str(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _MalformedEntry(f"{key} is missing or not a string")
    return value.strip()


def _required_int(entry: Mapping[str, Any], key: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _MalformedEntry(f"{key} is missing or not an integer")
    if value < 0:
        raise _MalformedEntry(f"{key} must not be negative")
    return value


def _onboard_date(entry: Mapping[str, Any]) -> int:
    """Bound the ms epoch so a bad onboardDate cannot crash serialization later."""
    value = _required_int(entry, "onboardDate")
    if not _MIN_ONBOARD_MS <= value <= _max_onboard_ms():
        raise _MalformedEntry("onboardDate is outside the supported date range")
    return value


def _filters(entry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = entry.get("filters")
    if not isinstance(raw, list):
        raise _MalformedEntry("filters is missing or not an array")
    by_type: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise _MalformedEntry("filters contains a non-object entry")
        filter_type = item.get("filterType")
        if isinstance(filter_type, str):
            by_type[filter_type] = item
    return by_type


def _filter_decimal(
    filters: Mapping[str, Mapping[str, Any]],
    filter_type: str,
    field: str,
    *,
    positive: bool,
) -> Decimal:
    item = filters.get(filter_type)
    if item is None:
        raise _MalformedEntry(f"missing {filter_type} filter")
    raw = item.get(field)
    if raw is None:
        raise _MalformedEntry(f"{filter_type}.{field} is missing")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise _MalformedEntry(f"{filter_type}.{field} is not a decimal") from exc
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise _MalformedEntry(f"{filter_type}.{field} is out of range")
    return value


def _parse_entry(entry: Any) -> SymbolContract:
    if not isinstance(entry, Mapping):
        raise _MalformedEntry("entry is not an object")

    symbol = _required_str(entry, "symbol")
    if _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise _MalformedEntry(f"invalid symbol {symbol!r}")

    contract_type = _required_str(entry, "contractType")
    status = _required_str(entry, "status")
    if contract_type not in ACCEPTED_CONTRACT_TYPES or status != _TRADING:
        raise _SkippedEntry

    filters = _filters(entry)
    tick_size = _filter_decimal(filters, "PRICE_FILTER", "tickSize", positive=True)
    step_size = _filter_decimal(filters, "LOT_SIZE", "stepSize", positive=True)
    min_qty = _filter_decimal(filters, "LOT_SIZE", "minQty", positive=True)
    min_notional = _filter_decimal(filters, "MIN_NOTIONAL", "notional", positive=False)

    underlying_raw = entry.get("underlyingType")
    underlying_type = (
        underlying_raw.strip()
        if isinstance(underlying_raw, str) and underlying_raw.strip()
        else None
    )

    return SymbolContract(
        symbol=symbol,
        base_asset=_required_str(entry, "baseAsset"),
        quote_asset=_required_str(entry, "quoteAsset"),
        contract_type=contract_type,
        underlying_type=underlying_type,
        status=status,
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
        price_precision=_required_int(entry, "pricePrecision"),
        quantity_precision=_required_int(entry, "quantityPrecision"),
        onboard_date=_onboard_date(entry),
        tradifi=contract_type == TRADIFI_CONTRACT_TYPE,
    )


class _SkippedEntry(Exception):
    """An entry is valid exchangeInfo but outside this registry's scope."""


def parse_exchange_info(payload: Any) -> ParseResult:
    """Parse an exchangeInfo payload, skipping unsupported and malformed entries."""
    if not isinstance(payload, Mapping):
        raise ExchangeInfoParseError("exchangeInfo payload must be a JSON object.")
    entries = payload.get("symbols")
    if not isinstance(entries, list):
        raise ExchangeInfoParseError("exchangeInfo payload has no symbols array.")

    contracts: dict[str, SymbolContract] = {}
    rejected: list[RejectedSymbol] = []
    for entry in entries:
        symbol = entry.get("symbol") if isinstance(entry, Mapping) else None
        rejected_symbol = symbol if isinstance(symbol, str) else None
        try:
            contract = _parse_entry(entry)
        except _SkippedEntry:
            continue
        except _MalformedEntry as exc:
            rejected.append(RejectedSymbol(symbol=rejected_symbol, reason=str(exc)))
            continue
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(RejectedSymbol(symbol=rejected_symbol, reason=str(exc)))
            continue
        # Last occurrence wins; the table primary key guarantees no duplicates.
        contracts[contract.symbol] = contract

    return ParseResult(contracts=tuple(contracts.values()), rejected=tuple(rejected))


class SymbolRegistry:
    """Ingest exchangeInfo and answer read-only symbol queries."""

    def __init__(self, store: StorePort, *, clock: ClockPort | None = None) -> None:
        self._store = store
        self._clock = clock or SystemClock()

    def refresh_from_payload(self, payload: Any) -> SymbolIngestResult:
        parsed = parse_exchange_info(payload)
        observed_at = utc_timestamp(self._clock.now())
        upsert = self._store.upsert_symbols(parsed.contracts, observed_at=observed_at)
        return _ingest_result(upsert, parsed.rejected)

    def refresh_from_fixture(self, path: Path | str) -> SymbolIngestResult:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SymbolRegistryError(f"cannot read exchangeInfo fixture: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExchangeInfoParseError(f"exchangeInfo fixture is not valid JSON: {exc}") from exc
        return self.refresh_from_payload(payload)

    async def refresh_from_exchange_info(self, source: ExchangeInfoSource) -> SymbolIngestResult:
        response = await source.fetch_exchange_info()
        return self.refresh_from_payload(response.payload)

    def list_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[SymbolContract, ...]:
        return self._store.list_symbols(
            query=query,
            tradifi=tradifi,
            contract_type=contract_type,
            limit=limit,
            offset=offset,
        )

    def count_symbols(
        self,
        *,
        query: str | None = None,
        tradifi: bool | None = None,
        contract_type: str | None = None,
    ) -> int:
        return self._store.count_symbols(query=query, tradifi=tradifi, contract_type=contract_type)

    def get_symbol(self, symbol: str) -> SymbolContract | None:
        return self._store.get_symbol(symbol.strip().upper())

    def count(self) -> int:
        return self._store.count_symbols()

    def group_counts(self) -> dict[str, int]:
        tradfi = self._store.count_symbols(tradifi=True)
        total = self._store.count_symbols()
        return {"crypto": total - tradfi, "tradfi": tradfi, "total": total}


def _ingest_result(
    upsert: SymbolUpsertResult, rejected: tuple[RejectedSymbol, ...]
) -> SymbolIngestResult:
    return SymbolIngestResult(
        inserted=upsert.inserted,
        updated=upsert.updated,
        unchanged=upsert.unchanged,
        total=upsert.total,
        rejected=rejected,
    )
