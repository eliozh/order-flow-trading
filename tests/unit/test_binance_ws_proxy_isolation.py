"""SEC: the Binance WebSocket transport must not consult ambient proxy settings.

websockets >= 15 defaults ``proxy=True`` on ``websockets.sync.client.connect``,
which makes the library read ``http_proxy``/``https_proxy``/``ALL_PROXY``/OS
proxy configuration. Market-data egress must be pinned to ``proxy=None``.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from oft.adapters.binance_ws import _SyncWebsocketTransport

_PROXY_ENV = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

_UNSET = object()


class _StubConnection:
    """Stands in for a ``ClientConnection``; no sockets, no network."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self, timeout: float | None = None) -> str:
        return ""

    def close(self) -> None:
        return None


def _transport() -> _SyncWebsocketTransport:
    return _SyncWebsocketTransport(
        "wss://fstream.binance.com/market/ws/btcusdt@aggTrade",
        max_size=1024,
        ping_interval=20.0,
        ping_timeout=20.0,
        open_timeout=10.0,
    )


def _poison_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXY_ENV:
        monkeypatch.setenv(name, "http://127.0.0.1:9")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    import websockets.sync.client as ws_client

    module: ModuleType = ws_client
    monkeypatch.setattr(module, "connect", fake)


def test_connect_forces_proxy_none_despite_hostile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _poison_proxy_environment(monkeypatch)
    recorded: dict[str, Any] = {}
    stub = _StubConnection()

    def fake_connect(uri: str, *, proxy: Any = _UNSET, **kwargs: Any) -> _StubConnection:
        recorded["uri"] = uri
        recorded["proxy"] = proxy
        recorded["kwargs"] = kwargs
        return stub

    _patch_connect(monkeypatch, fake_connect)
    transport = _transport()

    transport.connect()

    assert recorded["proxy"] is None
    assert transport._connection is stub
    assert recorded["kwargs"]["max_size"] == 1024
    assert recorded["kwargs"]["open_timeout"] == 10.0


def test_connect_omits_proxy_for_a_signature_that_predates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared floor is websockets>=13.0, which has no ``proxy`` kwarg."""
    _poison_proxy_environment(monkeypatch)
    recorded: dict[str, Any] = {}
    stub = _StubConnection()

    def old_connect(uri: str, **kwargs: Any) -> _StubConnection:
        recorded["uri"] = uri
        recorded["kwargs"] = kwargs
        return stub

    _patch_connect(monkeypatch, old_connect)
    transport = _transport()

    transport.connect()

    assert "proxy" not in recorded["kwargs"]
    assert transport._connection is stub
