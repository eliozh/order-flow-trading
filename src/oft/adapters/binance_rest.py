"""Minimal async Binance USD-M REST adapter (exchangeInfo only).

No credentials are used: ``GET /fapi/v1/exchangeInfo`` is public. The adapter
is weight-aware (reads ``X-MBX-USED-WEIGHT-1M``), bounded (response size cap),
and retries only transient failures with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://fapi.binance.com"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.25
DEFAULT_MAX_WEIGHT = 2_000
DEFAULT_MAX_SYMBOLS = 5_000

_WEIGHT_HEADER = "x-mbx-used-weight-1m"
_USER_AGENT = "oft/0.1 (+local)"


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


FetchCallable = Callable[[str, float, int], HttpResponse]
SleepCallable = Callable[[float], Awaitable[None]]


def default_fetch(url: str, timeout: float, max_bytes: int) -> HttpResponse:
    """Perform one blocking HTTP GET (called from a worker thread)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        fetch: FetchCallable = default_fetch,
        sleep: SleepCallable = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._max_weight = max_weight
        self._max_symbols = max_symbols
        self._fetch = fetch
        self._sleep = sleep

    @property
    def exchange_info_url(self) -> str:
        return f"{self._base_url}{EXCHANGE_INFO_PATH}"

    async def fetch_exchange_info(self) -> ExchangeInfoResponse:
        """GET exchangeInfo with bounded retries on transient failures."""
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
