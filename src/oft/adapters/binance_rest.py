"""Minimal async Binance USD-M REST adapter (exchangeInfo only).

No credentials are used: ``GET /fapi/v1/exchangeInfo`` is public. The adapter
is weight-aware (reads ``X-MBX-USED-WEIGHT-1M``), bounded (response size cap),
and retries only transient failures with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from pydantic import ValidationError

from oft.application.rate_limiter import GlobalRateLimiter
from oft.domain.events import AggTrade
from oft.ports.market_data import AggTradeRecovery

BASE_URL = "https://fapi.binance.com"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
AGG_TRADES_PATH = "/fapi/v1/aggTrades"
AGG_TRADES_LIMIT = 1_000
AGG_TRADES_WEIGHT = 20
AGG_TRADES_HISTORY_HOURS = 48
AGG_TRADES_MAX_WINDOW_SECONDS = 3_600
DEFAULT_MAX_REQUESTS = AGG_TRADES_HISTORY_HOURS
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.25
DEFAULT_MAX_WEIGHT = 2_000
DEFAULT_MAX_SYMBOLS = 5_000

_WEIGHT_HEADER = "x-mbx-used-weight-1m"
_USER_AGENT = "oft/0.1 (+local)"
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}$")
#: Market-data egress must not be rerouted by ambient proxy settings. The
#: default urllib opener reads ``http_proxy``/``ALL_PROXY``; an explicit empty
#: ``ProxyHandler`` keeps this adapter's requests direct.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class HttpResponse:
    """Transport-level response used to keep tests network-free."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class BinanceRestError(Exception):
    """A typed upstream failure; ``code`` matches ``error-catalog.json``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExchangeInfoResponse:
    """Validated exchangeInfo payload plus the observed request weight."""

    payload: dict[str, Any]
    weight_used: int | None


@dataclass(frozen=True)
class AggTradePage:
    """One validated ``aggTrades`` page plus the observed request weight."""

    trades: tuple[AggTrade, ...]
    weight_used: int | None


FetchCallable = Callable[[str, float, int], HttpResponse]
SleepCallable = Callable[[float], Awaitable[None]]


def default_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
    """Perform one blocking HTTP GET (called from a worker thread)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with _NO_PROXY_OPENER.open(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(max_bytes + 1),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers is not None else {},
            body=exc.read(max_bytes + 1),
        )


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _epoch_millis(value: datetime) -> int:
    if value.tzinfo is None:
        raise BinanceRestError("VALIDATION_FAILED", "timestamp must be timezone-aware")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _from_epoch_millis(value: int) -> datetime:
    if value < 0:
        raise ValueError("timestamp must be non-negative")
    return datetime.fromtimestamp(value // 1_000, tz=UTC) + timedelta(milliseconds=value % 1_000)


def _validate_agg_trade_window(
    symbol: str,
    start_time: datetime,
    end_time: datetime,
    limit: int,
    max_weight: int,
) -> None:
    if _SYMBOL_PATTERN.fullmatch(symbol or "") is None:
        raise BinanceRestError("VALIDATION_FAILED", "symbol is not a safe uppercase symbol")
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise BinanceRestError("VALIDATION_FAILED", "window bounds must be timezone-aware")
    if end_time <= start_time:
        raise BinanceRestError("VALIDATION_FAILED", "end_time must be after start_time")
    if (end_time - start_time).total_seconds() >= AGG_TRADES_MAX_WINDOW_SECONDS:
        raise BinanceRestError(
            "VALIDATION_FAILED",
            "Binance requires an aggTrades window shorter than one hour.",
        )
    if not 1 <= limit <= AGG_TRADES_LIMIT:
        raise BinanceRestError(
            "VALIDATION_FAILED", f"aggTrades limit must be between 1 and {AGG_TRADES_LIMIT}"
        )
    if max_weight <= 0:
        raise BinanceRestError("VALIDATION_FAILED", "max_weight must be positive")


class BinanceRestClient:
    """Fetch and validate ``exchangeInfo``; nothing else is implemented."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_weight: int = DEFAULT_MAX_WEIGHT,
        max_symbols: int = DEFAULT_MAX_SYMBOLS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        fetch: FetchCallable = default_fetch,
        sleep: SleepCallable = asyncio.sleep,
        rate_limiter: GlobalRateLimiter | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._max_weight = max_weight
        self._max_symbols = max_symbols
        self._max_requests = max_requests
        self._fetch = fetch
        self._sleep = sleep
        self._rate_limiter = rate_limiter

    def _acquire(self, path: str) -> None:
        if self._rate_limiter is None:
            return
        decision = self._rate_limiter.acquire(path)
        if not decision.allowed:
            raise BinanceRestError(
                "BINANCE_RATE_LIMITED",
                f"{decision.reason} for {path}; retry after {decision.retry_after_seconds:.1f}s",
            )

    def _note_status(self, path: str, status: int) -> None:
        if self._rate_limiter is not None:
            self._rate_limiter.note_status(status, endpoint=path)

    @property
    def exchange_info_url(self) -> str:
        return f"{self._base_url}{EXCHANGE_INFO_PATH}"

    async def fetch_exchange_info(self) -> ExchangeInfoResponse:
        """GET exchangeInfo with bounded retries on transient failures."""
        self._acquire(EXCHANGE_INFO_PATH)
        attempt = 0
        while True:
            try:
                response = await asyncio.to_thread(
                    self._fetch, self.exchange_info_url, self._timeout, self._max_bytes
                )
            except TimeoutError as exc:
                if attempt >= self._max_retries:
                    raise BinanceRestError(
                        "UPSTREAM_TIMEOUT", "Binance exchangeInfo request timed out."
                    ) from exc
                await self._sleep(self._backoff_seconds * (2**attempt))
                attempt += 1
                continue
            except OSError as exc:
                if attempt >= self._max_retries:
                    raise BinanceRestError(
                        "UPSTREAM_UNAVAILABLE", "Binance exchangeInfo request failed."
                    ) from exc
                await self._sleep(self._backoff_seconds * (2**attempt))
                attempt += 1
                continue

            self._note_status(EXCHANGE_INFO_PATH, response.status)
            if response.status in {418, 429}:
                raise BinanceRestError(
                    "BINANCE_RATE_LIMITED",
                    "Binance rate-limited the exchangeInfo request.",
                )
            if 500 <= response.status < 600:
                if attempt >= self._max_retries:
                    raise BinanceRestError(
                        "UPSTREAM_UNAVAILABLE",
                        f"Binance exchangeInfo returned HTTP {response.status}.",
                    )
                await self._sleep(self._backoff_seconds * (2**attempt))
                attempt += 1
                continue
            if response.status >= 400:
                raise BinanceRestError(
                    "UPSTREAM_UNAVAILABLE",
                    f"Binance exchangeInfo returned HTTP {response.status}.",
                )
            return self._parse(response)

    def fetch_agg_trades(
        self,
        symbol: str,
        *,
        start_time: datetime,
        end_time: datetime,
        limit: int = AGG_TRADES_LIMIT,
        from_id: int | None = None,
    ) -> AggTradePage:
        """GET one bounded ``aggTrades`` window (both bounds < 1 h apart).

        The REST endpoint only serves the last 48 h and rejects a start/end
        span of an hour or more, so callers must paginate. ``429``/``418`` and
        an over-cap weight header are surfaced as ``BINANCE_RATE_LIMITED``.
        """
        if from_id is None:
            _validate_agg_trade_window(symbol, start_time, end_time, limit, self._max_weight)
            query = urlencode(
                {
                    "symbol": symbol,
                    "startTime": _epoch_millis(start_time),
                    "endTime": _epoch_millis(end_time),
                    "limit": limit,
                }
            )
        else:
            query = urlencode({"symbol": symbol, "fromId": from_id, "limit": limit})
        self._acquire(AGG_TRADES_PATH)
        url = f"{self._base_url}{AGG_TRADES_PATH}?{query}"
        try:
            response = self._fetch(url, self._timeout, self._max_bytes)
        except TimeoutError as exc:
            raise BinanceRestError(
                "UPSTREAM_TIMEOUT", "Binance aggTrades request timed out."
            ) from exc
        except OSError as exc:
            raise BinanceRestError(
                "UPSTREAM_UNAVAILABLE", "Binance aggTrades request failed."
            ) from exc

        self._note_status(AGG_TRADES_PATH, response.status)
        if response.status in {418, 429}:
            raise BinanceRestError(
                "BINANCE_RATE_LIMITED", "Binance rate-limited the aggTrades request."
            )
        if response.status >= 400:
            raise BinanceRestError(
                "UPSTREAM_UNAVAILABLE", f"Binance aggTrades returned HTTP {response.status}."
            )
        weight = self._weight_from(response)
        return AggTradePage(
            trades=self._parse_agg_trades(response, symbol, limit),
            weight_used=weight,
        )

    def recover_agg_trades(
        self,
        symbol: str,
        *,
        last_event_time: datetime | None,
        now: datetime,
        stop: Callable[[], bool] | None = None,
    ) -> AggTradeRecovery:
        """Recover the last 48 h in one-hour windows with a request budget.

        ``stop`` is a cooperative shutdown hook: it is checked before each
        window and before each ``fromId`` page so a daemon stop does not wait
        for the whole 48 h scan. A stop returns the partial recovery with
        ``reason="SHUTDOWN"`` and ``complete=False``.
        """
        if now.tzinfo is None:
            raise BinanceRestError("VALIDATION_FAILED", "recovery time must be timezone-aware")
        end = now.astimezone(UTC)
        earliest = end - timedelta(hours=AGG_TRADES_HISTORY_HOURS)
        clamped = False
        if last_event_time is None:
            start = earliest
        else:
            if last_event_time.tzinfo is None:
                raise BinanceRestError(
                    "VALIDATION_FAILED", "checkpoint time must be timezone-aware"
                )
            candidate = last_event_time.astimezone(UTC) + timedelta(milliseconds=1)
            clamped = candidate < earliest
            start = max(candidate, earliest)

        trades: list[AggTrade] = []
        requests = 0
        weight_used: int | None = None
        reason: str | None = "RECOVERY_WINDOW_EXCEEDED_48H" if clamped else None
        stopped = False
        cursor = start
        while cursor < end:
            if stop is not None and stop():
                stopped = True
                break
            if requests >= self._max_requests:
                reason = reason or "RATE_LIMIT_BUDGET_EXHAUSTED"
                break
            window_end = min(
                cursor
                + timedelta(seconds=AGG_TRADES_MAX_WINDOW_SECONDS)
                - timedelta(milliseconds=1),
                end,
            )
            try:
                page = self.fetch_agg_trades(
                    symbol,
                    start_time=cursor,
                    end_time=window_end,
                )
            except BinanceRestError as exc:
                if exc.code == "BINANCE_RATE_LIMITED":
                    reason = reason or "BINANCE_RATE_LIMITED"
                    break
                raise
            requests += 1
            trades.extend(page.trades)
            weight_used = page.weight_used
            # A full page means the window may have been truncated: page with
            # fromId until the endpoint returns fewer than `limit` rows.
            while len(page.trades) >= AGG_TRADES_LIMIT:
                if stop is not None and stop():
                    stopped = True
                    break
                if requests >= self._max_requests:
                    reason = reason or "RATE_LIMIT_BUDGET_EXHAUSTED"
                    break
                next_from = max(trade.agg_trade_id for trade in page.trades) + 1
                page = self.fetch_agg_trades(
                    symbol,
                    start_time=cursor,
                    end_time=window_end,
                    from_id=next_from,
                )
                requests += 1
                weight_used = page.weight_used
                if not page.trades:
                    break
                trades.extend(page.trades)
            if stopped:
                break
            cursor = window_end
        if stopped:
            reason = "SHUTDOWN"
        elif cursor < end and reason is None and requests >= self._max_requests:
            reason = "RATE_LIMIT_BUDGET_EXHAUSTED"
        return AggTradeRecovery(
            trades=tuple(trades),
            requests=requests,
            weight_used=weight_used,
            complete=reason is None,
            reason=reason,
            start=start,
            end=end,
        )

    def _weight_from(self, response: HttpResponse) -> int | None:
        raw_weight = _lower_headers(response.headers).get(_WEIGHT_HEADER)
        weight: int | None = None
        if raw_weight is not None:
            try:
                weight = int(raw_weight)
            except ValueError:
                weight = None
        if weight is not None and weight > self._max_weight:
            raise BinanceRestError(
                "BINANCE_RATE_LIMITED",
                f"Binance request weight {weight} exceeds the cap {self._max_weight}.",
            )
        return weight

    def _parse_agg_trades(
        self, response: HttpResponse, symbol: str, limit: int
    ) -> tuple[AggTrade, ...]:
        if len(response.body) > self._max_bytes:
            raise BinanceRestError(
                "UPSTREAM_UNAVAILABLE",
                f"Binance aggTrades exceeded the {self._max_bytes} byte cap.",
            )
        try:
            payload: Any = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BinanceRestError(
                "SCHEMA_INVALID", "Binance aggTrades was not valid JSON."
            ) from exc
        if not isinstance(payload, list):
            raise BinanceRestError("SCHEMA_INVALID", "Binance aggTrades must be an array.")
        if len(payload) > limit:
            raise BinanceRestError(
                "SCHEMA_INVALID", f"Binance aggTrades exceeded the {limit} row cap."
            )
        trades: list[AggTrade] = []
        for row in payload:
            if not isinstance(row, list) or len(row) != 7:
                raise BinanceRestError(
                    "SCHEMA_INVALID", "Binance aggTrades row must have seven fields."
                )
            if not all(
                isinstance(row[index], int) and not isinstance(row[index], bool)
                for index in (0, 3, 4, 5)
            ):
                raise BinanceRestError(
                    "SCHEMA_INVALID", "Binance aggTrades ids/timestamps must be integers."
                )
            if not isinstance(row[6], bool):
                raise BinanceRestError(
                    "SCHEMA_INVALID", "Binance aggTrades is_buyer_maker must be a boolean."
                )
            try:
                trades.append(
                    AggTrade(
                        symbol=symbol,
                        agg_trade_id=int(row[0]),
                        price=Decimal(str(row[1])),
                        quantity=Decimal(str(row[2])),
                        first_trade_id=int(row[3]),
                        last_trade_id=int(row[4]),
                        transact_time=_from_epoch_millis(int(row[5])),
                        is_buyer_maker=row[6],
                    )
                )
            except (TypeError, ValueError, InvalidOperation, ValidationError) as exc:
                raise BinanceRestError(
                    "SCHEMA_INVALID", "Binance aggTrades row failed validation."
                ) from exc
        return tuple(trades)

    def _parse(self, response: HttpResponse) -> ExchangeInfoResponse:
        if len(response.body) > self._max_bytes:
            raise BinanceRestError(
                "UPSTREAM_UNAVAILABLE",
                f"Binance exchangeInfo exceeded the {self._max_bytes} byte cap.",
            )
        try:
            payload: Any = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BinanceRestError(
                "SCHEMA_INVALID", "Binance exchangeInfo was not valid JSON."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise BinanceRestError(
                "SCHEMA_INVALID",
                "Binance exchangeInfo is missing the symbols array.",
            )
        if len(payload["symbols"]) > self._max_symbols:
            raise BinanceRestError(
                "SCHEMA_INVALID",
                f"Binance exchangeInfo exceeded the {self._max_symbols} symbol cap.",
            )

        headers = _lower_headers(response.headers)
        raw_weight = headers.get(_WEIGHT_HEADER)
        weight: int | None = None
        if raw_weight is not None:
            try:
                weight = int(raw_weight)
            except ValueError:
                weight = None
        if weight is not None and weight > self._max_weight:
            raise BinanceRestError(
                "BINANCE_RATE_LIMITED",
                f"Binance request weight {weight} exceeds the cap {self._max_weight}.",
            )
        return ExchangeInfoResponse(payload=payload, weight_used=weight)
