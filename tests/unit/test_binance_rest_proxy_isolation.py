"""Regression: Binance REST market-data egress never uses an ambient proxy.

``default_fetch`` previously called ``urllib.request.urlopen`` directly, whose
default opener honors ``http_proxy``/``ALL_PROXY``. Ambient proxy settings must
not be able to intercept or redirect exchange market data.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from oft.adapters.binance_rest import default_fetch

_BODY = b'{"symbols": []}'
_PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


class _Handler(BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.requests.append({"path": self.path, "host": self.headers.get("Host")})
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, body: bytes = _BODY) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.body = body
        self.requests: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


@contextmanager
def _running(server: _RecordingServer) -> Iterator[_RecordingServer]:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_default_fetch_bypasses_a_hostile_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _running(_RecordingServer()) as upstream, _running(_RecordingServer()) as hostile:
        for name in _PROXY_ENV:
            monkeypatch.setenv(name, hostile.url)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # Force urllib to rebuild its default opener from the hostile environment
        # so the unpatched fetch demonstrably consults the proxy.
        monkeypatch.setattr(urllib.request, "_opener", None)

        response = default_fetch(f"{upstream.url}/fapi/v1/exchangeInfo", 2.0, 1_024)

        assert response.status == 200
        assert response.body == _BODY
        assert hostile.requests == []
        assert [request["path"] for request in upstream.requests] == ["/fapi/v1/exchangeInfo"]
