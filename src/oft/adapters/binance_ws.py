"""Binance USD-M WebSocket adapter with explicit stream routing.

The routing trap this module defends against: ``wss://fstream.binance.com/ws``
only delivers ``/public`` streams, while ``@aggTrade``, ``@kline_*``,
``@markPrice``, and ``@forceOrder`` are ``/market`` streams. Subscribing a
market stream on the bare path fails silently, so routing is explicit here and
a wrong path is detected, alerted, and refused.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import structlog

from oft.application.rate_limiter import WsSubscriptionBudget
from oft.domain.events import EventValidationError, parse_stream_message, strip_control_characters

logger = structlog.get_logger(__name__)

WS_BASE_URL = "wss://fstream.binance.com"
ROUTE_PUBLIC = "/public"
ROUTE_MARKET = "/market"
ROUTE_PRIVATE = "/private"

_MARKET_SUFFIXES = ("@aggtrade", "@kline_", "@markprice", "@forceorder")
_PUBLIC_SUFFIXES = ("@depth", "@bookticker", "@ticker", "!ticker@arr")
_BARE_PATHS = frozenset({"/ws", "/stream"})
_ROUTE_SEGMENTS = frozenset({"public", "market", "private"})

DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_RECEIVE_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 20.0
DEFAULT_ACK_TIMEOUT = 10.0


class WsError(Exception):
    """Base class for typed WebSocket failures."""

    code = "UPSTREAM_UNAVAILABLE"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = strip_control_characters(message)
        if code is not None:
            self.code = code


class WrongWsPathError(WsError):
    code = "WRONG_WS_PATH"


class SubscriptionAckError(WsError):
    code = "SILENT_SUBSCRIPTION_FAILURE"


class SubscriptionRejectedError(WsError):
    code = "SUBSCRIPTION_REJECTED"


class MessageTooLargeError(WsError):
    code = "OVERSIZED"


class ReceiveTimeoutError(WsError):
    code = "UPSTREAM_TIMEOUT"


@dataclass(frozen=True)
class RoutingAlert:
    """A detected path/stream mismatch that would otherwise fail silently."""

    code: str
    url: str
    streams: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class SubscriptionAck:
    """A validated SUBSCRIBE acknowledgment."""

    request_id: int
    result: tuple[str, ...]


def route_for_stream(stream: str) -> str:
    """Return the explicit Binance route for a stream name."""
    if not isinstance(stream, str) or not stream:
        raise ValueError("stream name is required")
    lowered = stream.lower()
    if lowered.startswith("listenkey") or "userdata" in lowered:
        return ROUTE_PRIVATE
    if any(lowered.endswith(suffix) or suffix in lowered for suffix in _MARKET_SUFFIXES):
        return ROUTE_MARKET
    if any(suffix in lowered for suffix in _PUBLIC_SUFFIXES):
        return ROUTE_PUBLIC
    raise ValueError(f"unknown stream route for {stream!r}; refusing to guess")


def ws_url(
    streams: Sequence[str],
    *,
    base_url: str = WS_BASE_URL,
) -> str:
    """Build a routed single/combined stream URL.

    Mixed routes cannot share one socket; refusing is safer than silently
    delivering only part of the subscription.
    """
    if not streams:
        raise ValueError("at least one stream is required")
    routes = {route_for_stream(stream) for stream in streams}
    if len(routes) != 1:
        raise ValueError("streams on different routes require separate connections")
    route = routes.pop()
    base = base_url.rstrip("/")
    if len(streams) == 1:
        return f"{base}{route}/ws/{streams[0]}"
    return f"{base}{route}/stream?streams={'/'.join(streams)}"


def detect_wrong_path(url: str, streams: Sequence[str]) -> RoutingAlert | None:
    """Detect a router mismatch before connecting (not after silent silence)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    segments = [segment for segment in path.split("/") if segment]
    expected = {route_for_stream(stream) for stream in streams}
    if not expected:
        return None

    bare = (
        path in _BARE_PATHS
        or path.startswith("/ws/")
        or path.startswith("/stream/")
        or not segments
    )
    if bare:
        expected_route = sorted(expected)[0]
        if any(route != ROUTE_PUBLIC for route in expected):
            return RoutingAlert(
                code="WRONG_WS_PATH",
                url=url,
                streams=tuple(streams),
                message=(
                    f"streams require {expected_route} but the URL uses the bare "
                    "/ws path, which delivers only /public streams and fails silently"
                ),
            )
        return None

    if segments and segments[0] in _ROUTE_SEGMENTS and f"/{segments[0]}" not in expected:
        return RoutingAlert(
            code="WRONG_WS_PATH",
            url=url,
            streams=tuple(streams),
            message=f"URL path {segments[0]} does not match the required stream route",
        )
    return None


TransportFactory = Callable[..., Any]


class _SyncWebsocketTransport:
    """Blocking ``websockets`` transport; imported lazily so tests stay offline."""

    def __init__(
        self,
        url: str,
        *,
        max_size: int,
        ping_interval: float,
        ping_timeout: float,
        open_timeout: float,
    ) -> None:
        self._url = url
        self._max_size = max_size
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._open_timeout = open_timeout
        self._connection: Any = None

    def connect(self) -> None:
        from websockets.sync.client import connect

        options: dict[str, Any] = {
            "max_size": self._max_size,
            "ping_interval": self._ping_interval,
            "ping_timeout": self._ping_timeout,
            "open_timeout": self._open_timeout,
            "close_timeout": 5,
        }
        # websockets >= 15 enables proxy support by default and would route
        # market-data egress through http_proxy/ALL_PROXY/OS settings. Disable
        # it explicitly. The declared floor is websockets>=13.0, which predates
        # the ``proxy`` parameter, so pass it only when the callable accepts it.
        if "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        self._connection = connect(self._url, **options)

    def send(self, text: str) -> None:
        if self._connection is None:
            raise WsError("WebSocket is not connected")
        self._connection.send(text)

    def recv(self, timeout: float | None = None) -> str:
        if self._connection is None:
            raise WsError("WebSocket is not connected")
        message = self._connection.recv(timeout=timeout)
        if isinstance(message, bytes):
            return message.decode("utf-8", errors="replace")
        return str(message)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def default_transport_factory(url: str, **kwargs: Any) -> _SyncWebsocketTransport:
    return _SyncWebsocketTransport(url, **kwargs)


def validate_subscription_ack(payload: Mapping[str, Any], *, expected_id: int) -> SubscriptionAck:
    """Validate one SUBSCRIBE acknowledgment; silence is a failure, not success."""
    if not isinstance(payload, Mapping):
        raise SubscriptionAckError("subscription acknowledgment was not an object")
    error = payload.get("error")
    if error is not None:
        if isinstance(error, Mapping):
            message = f"subscription rejected: {error.get('msg', 'unknown error')}"
        else:
            message = "subscription rejected"
        raise SubscriptionRejectedError(message)
    if payload.get("id") != expected_id:
        raise SubscriptionAckError("subscription acknowledgment id did not match")
    if "result" not in payload:
        raise SubscriptionAckError("subscription acknowledgment is missing result")
    result = payload.get("result")
    if result is None:
        return SubscriptionAck(request_id=expected_id, result=())
    if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
        raise SubscriptionAckError("subscription acknowledgment result is invalid")
    return SubscriptionAck(request_id=expected_id, result=tuple(result))


class BinanceWsClient:
    """Bounded, routed, acknowledgment-validating public stream client."""

    def __init__(
        self,
        *,
        base_url: str = WS_BASE_URL,
        transport_factory: TransportFactory = default_transport_factory,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT,
        subscription_budget: WsSubscriptionBudget | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport_factory = transport_factory
        self._max_message_bytes = max_message_bytes
        self._receive_timeout = receive_timeout
        self._connect_timeout = connect_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._ack_timeout = ack_timeout
        self._subscription_budget = subscription_budget
        self._transport: Any = None
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self._subscribed: tuple[str, ...] | None = None
        self._ack: SubscriptionAck | None = None
        self._alerts: list[RoutingAlert] = []
        self.connection_state = "DISCONNECTED"
        self._counters: dict[str, int] = {"messages": 0, "oversized": 0, "alerts": 0}

    @property
    def alerts(self) -> tuple[RoutingAlert, ...]:
        return tuple(self._alerts)

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def connect(self, streams: Sequence[str], *, url: str | None = None) -> None:
        """Connect with explicit routing; refuse a path that would fail silently."""
        target = url or ws_url(streams, base_url=self._base_url)
        alert = detect_wrong_path(target, streams)
        if alert is not None:
            self._alerts.append(alert)
            self._counters["alerts"] += 1
            logger.error(
                "ws_routing_alert",
                code=alert.code,
                streams=list(alert.streams),
                url=strip_control_characters(alert.url)[:512],
            )
            raise WrongWsPathError(alert.message)
        if self._subscription_budget is not None:
            self._subscription_budget.reset()
            if not self._subscription_budget.can_subscribe(len(streams)):
                raise SubscriptionRejectedError("subscription budget exceeded for this connection")
        self.close()
        self.connection_state = "CONNECTING"
        try:
            transport = self._transport_factory(
                target,
                max_size=self._max_message_bytes,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                open_timeout=self._connect_timeout,
            )
            transport.connect()
            self._transport = transport
            self.subscribe(streams)
            if self._subscription_budget is not None:
                self._subscription_budget.consume(len(streams))
            self.connection_state = "SUBSCRIBED"
        except Exception:
            self.close()
            self.connection_state = "FAILED"
            raise

    def subscribe(
        self,
        streams: Sequence[str],
        *,
        request_id: int = 1,
        timeout: float | None = None,
    ) -> SubscriptionAck:
        """Send SUBSCRIBE and require a matching acknowledgment."""
        if self._transport is None:
            raise WsError("WebSocket is not connected")
        key = tuple(streams)
        if self._ack is not None and self._subscribed == key and self._ack.request_id == request_id:
            return self._ack
        request = {"method": "SUBSCRIBE", "params": list(streams), "id": request_id}
        self._transport.send(json.dumps(request))
        deadline = time.monotonic() + (timeout if timeout is not None else self._ack_timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SubscriptionAckError(
                    "subscription acknowledgment was not received",
                    code="SILENT_SUBSCRIPTION_FAILURE",
                )
            try:
                raw = self._transport.recv(timeout=remaining)
            except TimeoutError as exc:
                raise SubscriptionAckError(
                    "subscription acknowledgment was not received",
                    code="SILENT_SUBSCRIPTION_FAILURE",
                ) from exc
            self._check_size(raw)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SubscriptionAckError(
                    "unexpected non-JSON message before the subscription acknowledgment"
                ) from exc
            if isinstance(payload, dict) and payload.get("id") == request_id:
                ack = validate_subscription_ack(payload, expected_id=request_id)
                self._ack = ack
                self._subscribed = key
                return ack
            # A stream event can arrive before the ack; keep it for the caller.
            try:
                stream, data = parse_stream_message(raw, max_bytes=self._max_message_bytes)
            except EventValidationError:
                continue
            if len(self._pending) < 10_000:
                self._pending.append((stream, data))

    def receive_event(self, timeout: float | None = None) -> tuple[str, dict[str, Any]]:
        """Receive one bounded, parsed stream event."""
        if self._pending:
            stream, payload = self._pending.pop(0)
            return stream, payload
        if self._transport is None:
            raise WsError("WebSocket is not connected")
        try:
            raw = self._transport.recv(
                timeout=timeout if timeout is not None else self._receive_timeout
            )
        except TimeoutError as exc:
            raise ReceiveTimeoutError("WebSocket receive timed out") from exc
        self._check_size(raw)
        self._counters["messages"] += 1
        try:
            return parse_stream_message(raw, max_bytes=self._max_message_bytes)
        except EventValidationError:
            raise

    def ingest_message(self, raw: str) -> tuple[str, dict[str, Any]]:
        """Validate a raw message without a transport (used by bounded tests)."""
        self._check_size(raw)
        self._counters["messages"] += 1
        return parse_stream_message(raw, max_bytes=self._max_message_bytes)

    def _check_size(self, raw: str) -> None:
        size = len(raw) if isinstance(raw, bytes) else len(str(raw).encode("utf-8"))
        if size > self._max_message_bytes:
            self._counters["oversized"] += 1
            raise MessageTooLargeError(f"message exceeds the {self._max_message_bytes} byte bound")

    def close(self) -> None:
        transport = self._transport
        self._transport = None
        self._ack = None
        self._subscribed = None
        self._pending.clear()
        if transport is not None:
            try:
                transport.close()
            finally:
                self.connection_state = "CLOSED"


__all__ = [
    "ROUTE_MARKET",
    "ROUTE_PRIVATE",
    "ROUTE_PUBLIC",
    "WS_BASE_URL",
    "BinanceWsClient",
    "MessageTooLargeError",
    "ReceiveTimeoutError",
    "RoutingAlert",
    "SubscriptionAck",
    "SubscriptionAckError",
    "SubscriptionRejectedError",
    "WrongWsPathError",
    "WsError",
    "detect_wrong_path",
    "route_for_stream",
    "validate_subscription_ack",
    "ws_url",
]
