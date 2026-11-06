"""Symbol parsing and the exchangeInfo adapter (fixture-only, no network)."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.binance_rest import BinanceRestClient, BinanceRestError, HttpResponse
from oft.application.symbol_registry import (
    ExchangeInfoParseError,
    SymbolIngestResult,
    SymbolRegistry,
    parse_exchange_info,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "exchange_info_sample.json"


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _ok_response(body: bytes | None = None, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={"Content-Type": "application/json", **headers},
        body=body if body is not None else _fixture_bytes(),
    )


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_accepts_perpetual_and_tradifi_perpetual() -> None:
    result = parse_exchange_info(_payload())
    contracts = {contract.symbol: contract for contract in result.contracts}

    assert set(contracts) == {"BTCUSDT", "ETHUSDT", "XAUUSDT"}

    btc = contracts["BTCUSDT"]
    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USDT"
    assert btc.contract_type == "PERPETUAL"
    assert btc.underlying_type == "COIN"
    assert btc.status == "TRADING"
    assert btc.tick_size == Decimal("0.10")
    assert btc.step_size == Decimal("0.001")
    assert btc.min_qty == Decimal("0.001")
    assert btc.min_notional == Decimal("5.00")
    assert btc.price_precision == 2
    assert btc.quantity_precision == 3
    assert btc.onboard_date == 1567900800000
    assert btc.listed_date == "2019-09-08"
    assert btc.tradifi is False

    xau = contracts["XAUUSDT"]
    assert xau.contract_type == "TRADIFI_PERPETUAL"
    assert xau.underlying_type == "COMMODITY"
    assert xau.tick_size == Decimal("0.01")
    assert xau.step_size == Decimal("0.01")
    assert xau.min_qty == Decimal("0.01")
    assert xau.min_notional == Decimal("5.00")
    assert xau.price_precision == 2
    assert xau.quantity_precision == 2
    assert xau.listed_date == "2025-12-11"
    assert xau.tradifi is True


def test_parse_filters_non_trading_and_unsupported_contract_types() -> None:
    result = parse_exchange_info(_payload())
    symbols = {contract.symbol for contract in result.contracts}

    assert "FTTUSDT" not in symbols  # status != TRADING
    assert "BTCUSDT_261225" not in symbols  # unsupported contract type
    assert {contract.status for contract in result.contracts} == {"TRADING"}


def test_parse_rejects_malformed_entries_without_crashing() -> None:
    result = parse_exchange_info(_payload())
    rejected = {entry.symbol for entry in result.rejected}

    assert {"BROKENUSDT", "BADTICKUSDT", "NODATEUSDT"} <= rejected
    assert None in rejected  # the non-object list element
    assert all(entry.reason for entry in result.rejected)


@pytest.mark.parametrize(
    "payload",
    [
        {"notSymbols": []},
        {"symbols": "not-a-list"},
        [],
        "not-a-mapping",
    ],
)
def test_parse_rejects_a_malformed_payload(payload: Any) -> None:
    with pytest.raises(ExchangeInfoParseError) as excinfo:
        parse_exchange_info(payload)

    assert excinfo.value.code == "SCHEMA_INVALID"


def test_parse_rejects_duplicate_symbols_first_wins() -> None:
    payload = _payload()
    payload["symbols"].append(
        {
            "symbol": "BTCUSDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "underlyingType": "COIN",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "onboardDate": 1567900800000,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.50"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.00"},
            ],
        }
    )

    result = parse_exchange_info(payload)
    contracts = [contract for contract in result.contracts if contract.symbol == "BTCUSDT"]

    assert len(contracts) == 1
    assert contracts[0].tick_size == Decimal("0.10")
    assert any(item.symbol == "BTCUSDT" and "duplicate" in item.reason for item in result.rejected)


def _btc_entry(payload: dict[str, Any]) -> dict[str, Any]:
    entry = next(item for item in payload["symbols"] if item.get("symbol") == "BTCUSDT")
    return copy.deepcopy(entry)


@pytest.mark.parametrize("onboard_date", [10**18, 0, 1_400_000_000_000])
def test_parse_rejects_out_of_range_onboard_dates(onboard_date: int) -> None:
    payload = _payload()
    entry = _btc_entry(payload)
    entry["symbol"] = "WEIRDUSDT"
    entry["onboardDate"] = onboard_date
    payload["symbols"].append(entry)

    result = parse_exchange_info(payload)

    assert "WEIRDUSDT" not in {contract.symbol for contract in result.contracts}
    rejected = {item.symbol: item.reason for item in result.rejected}
    assert "WEIRDUSDT" in rejected
    assert "onboardDate" in rejected["WEIRDUSDT"]


def test_parse_accepts_an_onboard_date_inside_the_range() -> None:
    result = parse_exchange_info(_payload())

    btc = next(contract for contract in result.contracts if contract.symbol == "BTCUSDT")
    assert btc.listed_date == "2019-09-08"


@pytest.mark.parametrize(
    ("field", "value"),
    [("pricePrecision", 21), ("quantityPrecision", 2**63), ("pricePrecision", 10**18)],
)
def test_parse_rejects_out_of_range_precision(field: str, value: int) -> None:
    payload = _payload()
    entry = _btc_entry(payload)
    entry["symbol"] = "PRECUSDT"
    entry[field] = value
    payload["symbols"].append(entry)

    result = parse_exchange_info(payload)

    assert "PRECUSDT" not in {contract.symbol for contract in result.contracts}
    rejected = {item.symbol: item.reason for item in result.rejected}
    assert field in rejected["PRECUSDT"]


def test_parse_rejects_out_of_range_decimal_magnitude() -> None:
    payload = _payload()
    entry = _btc_entry(payload)
    entry["symbol"] = "HUGEUSDT"
    for item in entry["filters"]:
        if item.get("filterType") == "PRICE_FILTER":
            item["tickSize"] = "1E+50"
    payload["symbols"].append(entry)

    result = parse_exchange_info(payload)

    assert "HUGEUSDT" not in {contract.symbol for contract in result.contracts}
    assert any(item.symbol == "HUGEUSDT" for item in result.rejected)


def test_unsupported_contract_is_skipped_not_rejected() -> None:
    result = parse_exchange_info(_payload())

    rejected = {item.symbol for item in result.rejected}
    assert "BTCUSDT_261225" not in rejected
    assert "BTCUSDT_261225" not in {contract.symbol for contract in result.contracts}


def test_onboard_upper_bound_uses_the_injected_clock() -> None:
    payload = _payload()
    entry = _btc_entry(payload)
    entry["symbol"] = "FUTUREUSDT"
    entry["onboardDate"] = int(datetime(2025, 6, 1, tzinfo=UTC).timestamp() * 1000)
    payload["symbols"].append(entry)

    early = parse_exchange_info(payload, now=datetime(2025, 1, 1, tzinfo=UTC))
    assert "FUTUREUSDT" not in {contract.symbol for contract in early.contracts}
    assert any(item.symbol == "FUTUREUSDT" for item in early.rejected)

    late = parse_exchange_info(payload, now=datetime(2025, 6, 2, tzinfo=UTC))
    assert "FUTUREUSDT" in {contract.symbol for contract in late.contracts}


def test_parser_enforces_the_symbol_row_cap() -> None:
    with pytest.raises(ExchangeInfoParseError) as excinfo:
        parse_exchange_info({"symbols": [{}, {}, {}]}, max_symbols=2)

    assert excinfo.value.code == "SCHEMA_INVALID"


def test_async_refresh_runs_parsing_and_writes_off_the_event_loop() -> None:
    class _Source:
        async def fetch_exchange_info(self) -> Any:
            return type("Response", (), {"payload": {"symbols": []}})()

    class _Store:
        def upsert_symbols(self, contracts: Any, *, observed_at: str) -> Any:
            raise AssertionError("patched refresh should not reach the store")

    registry: Any = SymbolRegistry(_Store())
    seen: dict[str, int] = {}

    def fake_refresh(payload: Any) -> SymbolIngestResult:
        seen["thread"] = threading.get_ident()
        return SymbolIngestResult(inserted=0, updated=0, unchanged=0, total=0)

    registry.refresh_from_payload = fake_refresh
    result = asyncio.run(registry.refresh_from_exchange_info(_Source()))

    assert result.total == 0
    assert seen["thread"] != threading.get_ident()


# ---------------------------------------------------------------------------
# Async adapter
# ---------------------------------------------------------------------------


def test_client_fetches_only_exchange_info_and_records_weight() -> None:
    calls: list[str] = []

    def fake_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        calls.append(url)
        return _ok_response(**{"X-MBX-USED-WEIGHT-1M": "12"})

    client = BinanceRestClient(fetch=fake_fetch)
    response = _run(client.fetch_exchange_info())

    assert calls == ["https://fapi.binance.com/fapi/v1/exchangeInfo"]
    assert response.weight_used == 12
    assert "symbols" in response.payload


def test_client_retries_a_server_error_then_succeeds() -> None:
    responses = [
        HttpResponse(status=503, headers={}, body=b"unavailable"),
        _ok_response(),
    ]
    delays: list[float] = []

    def fake_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        return responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = BinanceRestClient(fetch=fake_fetch, sleep=fake_sleep, max_retries=1)
    response = _run(client.fetch_exchange_info())

    assert "symbols" in response.payload
    assert delays == [0.25]


def test_client_retries_a_timeout_then_succeeds() -> None:
    attempts = {"count": 0}

    def fake_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError("timed out")
        return _ok_response()

    async def fake_sleep(delay: float) -> None:
        return None

    client = BinanceRestClient(fetch=fake_fetch, sleep=fake_sleep, max_retries=1)
    response = _run(client.fetch_exchange_info())

    assert attempts["count"] == 2
    assert "symbols" in response.payload


def test_client_reports_rate_limiting() -> None:
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: HttpResponse(status=429, headers={}, body=b"{}")
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "BINANCE_RATE_LIMITED"


def test_client_reports_used_weight_over_the_cap() -> None:
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: _ok_response(**{"X-MBX-USED-WEIGHT-1M": "1300"}),
        max_weight=1200,
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "BINANCE_RATE_LIMITED"


def test_client_rejects_a_response_over_the_size_cap() -> None:
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: HttpResponse(status=200, headers={}, body=b"x" * 11),
        max_bytes=10,
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"


def test_client_rejects_invalid_json() -> None:
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: HttpResponse(status=200, headers={}, body=b"not-json")
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "SCHEMA_INVALID"


def test_client_rejects_a_payload_without_symbols() -> None:
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: HttpResponse(
            status=200, headers={}, body=b'{"foo": 1}'
        )
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "SCHEMA_INVALID"


def test_client_rejects_a_payload_over_the_symbol_row_cap() -> None:
    body = json.dumps({"symbols": [{} for _ in range(11)]}).encode("utf-8")
    client = BinanceRestClient(
        fetch=lambda url, timeout, max_bytes: HttpResponse(status=200, headers={}, body=body),
        max_symbols=10,
    )

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "SCHEMA_INVALID"
    assert "10 symbol cap" in excinfo.value.message


def test_client_exhausts_retries_on_server_errors() -> None:
    attempts = {"count": 0}

    def fake_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
        attempts["count"] += 1
        return HttpResponse(status=502, headers={}, body=b"bad gateway")

    async def fake_sleep(delay: float) -> None:
        return None

    client = BinanceRestClient(fetch=fake_fetch, sleep=fake_sleep, max_retries=2)

    with pytest.raises(BinanceRestError) as excinfo:
        _run(client.fetch_exchange_info())

    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"
    assert attempts["count"] == 3


def test_parse_keeps_first_duplicate_filter_and_records_it() -> None:
    payload = _payload()
    entry = _btc_entry(payload)
    entry["symbol"] = "DUPFILTERUSDT"
    entry["filters"].append({"filterType": "PRICE_FILTER", "tickSize": "0.20"})
    payload["symbols"].append(entry)

    result = parse_exchange_info(payload)

    contract = next((item for item in result.contracts if item.symbol == "DUPFILTERUSDT"), None)
    assert contract is not None  # the symbol is still accepted
    assert contract.tick_size == Decimal("0.10")  # first filter wins
    assert any(
        item.symbol == "DUPFILTERUSDT" and "duplicate PRICE_FILTER" in item.reason
        for item in result.rejected
    )
