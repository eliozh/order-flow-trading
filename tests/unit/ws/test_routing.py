"""WS routing, acknowledgment, bounded receive, and normalization tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.binance_ws import (
    BinanceWsClient,
    MessageTooLargeError,
    ReceiveTimeoutError,
    SubscriptionAckError,
    SubscriptionRejectedError,
    WrongWsPathError,
    detect_wrong_path,
    route_for_stream,
    ws_url,
)
from oft.domain.events import (
    EventValidationError,
    ForceOrderEvent,
    KlineEvent,
    MarkPriceEvent,
    normalize_event,
    parse_stream_message,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ws"


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()


class FakeTransport:
    """Deterministic in-memory transport; no sockets, no network."""

    def __init__(self, messages: list[str], *, timeout: bool = False) -> None:
        self.messages = list(messages)
        self.timeout = timeout
        self.sent: list[str] = []
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self, timeout: float | None = None) -> str:
        if self.timeout or not self.messages:
            raise TimeoutError("no message before timeout")
        return self.messages.pop(0)

    def close(self) -> None:
        self.closed = True


def _client(messages: list[str], **overrides: Any) -> tuple[BinanceWsClient, FakeTransport]:
    transport = FakeTransport(messages)
    client = BinanceWsClient(
        transport_factory=lambda *_args, **_kwargs: transport,
        **overrides,
    )
    return client, transport


def test_market_streams_route_to_the_market_path() -> None:
    for stream in (
        "btcusdt@aggTrade",
        "btcusdt@kline_1m",
        "btcusdt@markPrice@1s",
        "btcusdt@forceOrder",
    ):
        assert route_for_stream(stream) == "/market"
        assert "/market/" in ws_url([stream])


def test_high_frequency_public_stream_routes_to_public_path() -> None:
    assert route_for_stream("btcusdt@depth@100ms") == "/public"
    assert "/public/" in ws_url(["btcusdt@depth@100ms"])


def test_mixed_routing_is_rejected_instead_of_silently_misrouted() -> None:
    with pytest.raises(ValueError):
        ws_url(["btcusdt@aggTrade", "btcusdt@depth@100ms"])


def test_subscription_ack_is_validated() -> None:
    ack = {"result": None, "id": 1}
    client, _ = _client([json.dumps(ack)])

    client.connect(["btcusdt@aggTrade"])

    assert client.connection_state == "SUBSCRIBED"


def test_silent_subscription_failure_is_rejected() -> None:
    client, _ = _client([], receive_timeout=0.01)

    with pytest.raises(SubscriptionAckError) as error:
        client.connect(["btcusdt@aggTrade"])

    assert error.value.code == "SILENT_SUBSCRIPTION_FAILURE"
    assert client.connection_state == "FAILED"


def test_error_ack_is_rejected_and_strips_control_characters() -> None:
    payload = {"error": {"code": -1121, "msg": "Invalid\u001b[31m symbol."}, "id": 1}
    client, _ = _client([json.dumps(payload)])

    with pytest.raises(SubscriptionRejectedError) as error:
        client.connect(["btcusdt@aggTrade"])

    assert "Invalid symbol." in error.value.message
    assert "\u001b" not in error.value.message


def test_bare_ws_path_for_market_stream_is_detected_and_alerted() -> None:
    url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
    alert = detect_wrong_path(url, ["btcusdt@aggTrade"])

    assert alert is not None
    assert alert.code == "WRONG_WS_PATH"
    client, transport = _client([json.dumps({"result": None, "id": 1})])
    with pytest.raises(WrongWsPathError):
        client.connect(["btcusdt@aggTrade"], url=url)
    assert client.alerts and client.alerts[0].code == "WRONG_WS_PATH"
    assert transport.connected is False


def test_oversized_message_is_rejected_with_a_counter() -> None:
    client, _ = _client([json.dumps({"result": None, "id": 1})], max_message_bytes=64)
    client.connect(["btcusdt@aggTrade"])

    with pytest.raises(MessageTooLargeError):
        client.ingest_message("x" * 4096)

    assert client.counters["oversized"] == 1


def test_receive_timeout_is_explicit() -> None:
    transport = FakeTransport([json.dumps({"result": None, "id": 1})])
    client = BinanceWsClient(transport_factory=lambda *_a, **_k: transport)
    client.connect(["btcusdt@aggTrade"])
    transport.timeout = True

    with pytest.raises(ReceiveTimeoutError):
        client.receive_event(timeout=0.01)


def test_all_four_streams_normalize_to_canonical_events() -> None:
    first = {name: _lines(name)[0] for name in ("aggtrade", "kline", "markprice", "forceorder")}
    parsed = {name: normalize_event(parse_stream_message(raw)[1]) for name, raw in first.items()}

    assert parsed["aggtrade"].symbol == "BTCUSDT"
    assert isinstance(parsed["kline"], KlineEvent)
    assert parsed["kline"].interval == "1m"
    assert isinstance(parsed["markprice"], MarkPriceEvent)
    assert parsed["markprice"].mark_price > 0
    assert isinstance(parsed["forceorder"], ForceOrderEvent)
    assert parsed["forceorder"].side == "SELL"


def test_control_characters_are_stripped_not_stored() -> None:
    raw = _lines("aggtrade")[6]
    stream, payload = parse_stream_message(raw)
    event = normalize_event(payload)

    assert stream == "btcusdt@aggTrade"
    assert event.symbol == "BTCUSDT"
    assert "\u0001" not in event.symbol


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(EventValidationError) as error:
        parse_stream_message("{not-json")

    assert error.value.reason == "MALFORMED_JSON"


def test_oversized_payload_is_rejected_before_parsing() -> None:
    with pytest.raises(EventValidationError) as error:
        parse_stream_message("x" * 512, max_bytes=64)

    assert error.value.reason == "OVERSIZED"


@pytest.mark.parametrize(
    ("fixture", "line"),
    [
        ("aggtrade", 4),
        ("aggtrade", 5),
        ("kline", 2),
        ("kline", 3),
        ("markprice", 2),
        ("forceorder", 2),
    ],
)
def test_invalid_fields_raise_validation_errors(fixture: str, line: int) -> None:
    _, payload = parse_stream_message(_lines(fixture)[line])

    with pytest.raises(EventValidationError):
        normalize_event(payload)


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(EventValidationError) as error:
        normalize_event({"e": "depthUpdate", "s": "BTCUSDT"})

    assert error.value.reason == "UNKNOWN_EVENT"
