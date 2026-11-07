"""REST aggTrades gap recovery with 48 h bound and rate-limit awareness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from oft.adapters.binance_rest import BinanceRestClient, BinanceRestError, HttpResponse

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _row(agg_trade_id: int, timestamp_ms: int = 1793966400000) -> list[object]:
    return [agg_trade_id, "35000.00", "0.100", agg_trade_id, agg_trade_id, timestamp_ms, False]


class FakeFetch:
    def __init__(self, *, status: int = 200, weight: int = 20) -> None:
        self.status = status
        self.weight = weight
        self.urls: list[str] = []
        self.bodies: list[bytes] = []

    def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        self.urls.append(url)
        body = json.dumps([_row(1)]).encode("utf-8")
        self.bodies.append(body)
        return HttpResponse(
            status=self.status,
            headers={"X-MBX-USED-WEIGHT-1M": str(self.weight)},
            body=body,
        )


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_recovery_uses_one_hour_windows_within_the_48h_bound() -> None:
    fetch = FakeFetch()
    client = BinanceRestClient(fetch=fetch)

    result = client.recover_agg_trades(
        "BTCUSDT",
        last_event_time=NOW - timedelta(hours=2, minutes=30),
        now=NOW,
    )

    assert result.complete is True
    assert result.reason is None
    assert result.requests == 3
    assert result.weight_used == 20
    assert [trade.agg_trade_id for trade in result.trades] == [1, 1, 1]
    for url in fetch.urls:
        query = _query(url)
        start = int(query["startTime"][0])
        end = int(query["endTime"][0])
        assert start < end
        assert end - start <= 3_600_000
        assert query["limit"] == ["1000"]
        assert "/fapi/v1/aggTrades" in url


def test_recovery_older_than_48h_is_marked_incomplete() -> None:
    fetch = FakeFetch()
    client = BinanceRestClient(fetch=fetch, max_requests=2)

    result = client.recover_agg_trades(
        "BTCUSDT",
        last_event_time=NOW - timedelta(hours=49),
        now=NOW,
    )

    assert result.complete is False
    assert result.reason == "RECOVERY_WINDOW_EXCEEDED_48H"
    assert result.requests == 2
    assert all(
        int(_query(url)["startTime"][0]) >= int((NOW - timedelta(hours=48)).timestamp() * 1000)
        for url in fetch.urls
    )


def test_rate_limit_budget_exhaustion_is_reported_without_retrying_forever() -> None:
    fetch = FakeFetch()
    client = BinanceRestClient(fetch=fetch, max_requests=1)

    result = client.recover_agg_trades(
        "BTCUSDT",
        last_event_time=NOW - timedelta(hours=2),
        now=NOW,
    )

    assert result.complete is False
    assert result.reason == "RATE_LIMIT_BUDGET_EXHAUSTED"
    assert result.requests == 1
    assert len(fetch.urls) == 1


def test_429_is_reported_as_binance_rate_limited() -> None:
    client = BinanceRestClient(fetch=FakeFetch(status=429))

    with pytest.raises(BinanceRestError) as error:
        client.fetch_agg_trades(
            "BTCUSDT",
            start_time=NOW - timedelta(minutes=10),
            end_time=NOW,
        )

    assert error.value.code == "BINANCE_RATE_LIMITED"


def test_weight_header_over_cap_is_rate_limited() -> None:
    client = BinanceRestClient(fetch=FakeFetch(weight=3_000))

    with pytest.raises(BinanceRestError) as error:
        client.fetch_agg_trades(
            "BTCUSDT",
            start_time=NOW - timedelta(minutes=10),
            end_time=NOW,
        )

    assert error.value.code == "BINANCE_RATE_LIMITED"


def test_malformed_payload_is_rejected_not_coerced() -> None:
    class BadFetch(FakeFetch):
        def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
            return HttpResponse(status=200, headers={}, body=b'{"not":"a list"}')

    client = BinanceRestClient(fetch=BadFetch())

    with pytest.raises(BinanceRestError) as error:
        client.fetch_agg_trades(
            "BTCUSDT",
            start_time=NOW - timedelta(minutes=10),
            end_time=NOW,
        )

    assert error.value.code == "SCHEMA_INVALID"


def test_non_boolean_maker_flag_is_rejected() -> None:
    class StringMakerFetch(FakeFetch):
        def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
            return HttpResponse(
                status=200,
                headers={"X-MBX-USED-WEIGHT-1M": "20"},
                body=json.dumps([[1, "35000.00", "0.1", 1, 1, 1793966400000, "false"]]).encode(
                    "utf-8"
                ),
            )

    client = BinanceRestClient(fetch=StringMakerFetch())

    with pytest.raises(BinanceRestError) as error:
        client.fetch_agg_trades(
            "BTCUSDT",
            start_time=NOW - timedelta(minutes=10),
            end_time=NOW,
        )

    assert error.value.code == "SCHEMA_INVALID"


def test_span_of_one_hour_or_more_is_rejected_before_the_request() -> None:
    client = BinanceRestClient(fetch=FakeFetch())

    with pytest.raises(BinanceRestError):
        client.fetch_agg_trades(
            "BTCUSDT",
            start_time=NOW - timedelta(hours=2),
            end_time=NOW,
        )


def test_unsafe_symbol_is_rejected() -> None:
    client = BinanceRestClient(fetch=FakeFetch())

    with pytest.raises(BinanceRestError):
        client.fetch_agg_trades(
            "BTCUSDT&limit=1000",
            start_time=NOW - timedelta(minutes=10),
            end_time=NOW,
        )


class PagedFetch:
    def __init__(self, *, always_full: bool = False) -> None:
        self.urls: list[str] = []
        self.always_full = always_full
        self.calls = 0

    def __call__(self, url: str, timeout: float, max_bytes: int) -> HttpResponse:
        self.urls.append(url)
        self.calls += 1
        start = int(parse_qs(urlsplit(url).query)["fromId"][0]) if "fromId" in url else 1
        count = 1000 if self.always_full or self.calls == 1 else 5
        body = json.dumps([_row(start + offset) for offset in range(count)]).encode()
        return HttpResponse(
            status=200,
            headers={"X-MBX-USED-WEIGHT-1M": "2"},
            body=body,
        )


def test_full_window_is_paged_with_from_id_until_short_page() -> None:
    fetch = PagedFetch()
    client = BinanceRestClient(fetch=fetch)

    result = client.recover_agg_trades("BTCUSDT", last_event_time=NOW - timedelta(hours=1), now=NOW)

    assert result.complete is True
    assert result.requests == 2
    from_id_queries = [_query(url) for url in fetch.urls if "fromId" in url]
    assert from_id_queries and from_id_queries[0]["fromId"] == ["1001"]
    assert len(result.trades) == 1005


def test_full_pages_until_the_request_budget_are_incomplete() -> None:
    fetch = PagedFetch(always_full=True)
    client = BinanceRestClient(fetch=fetch, max_requests=3)

    result = client.recover_agg_trades("BTCUSDT", last_event_time=NOW - timedelta(hours=1), now=NOW)

    assert result.complete is False
    assert result.reason == "RATE_LIMIT_BUDGET_EXHAUSTED"
