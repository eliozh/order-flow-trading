"""Regression: the CLI API client never routes loopback traffic through a proxy.

``urllib.request.urlopen`` builds its default opener from the ambient proxy
environment. A loopback request forwarded as an absolute-URI request would send
``Authorization: Bearer <OFT_API_TOKEN>`` to whatever ``http_proxy``/
``ALL_PROXY`` names. Both request paths must bypass proxies entirely.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from oft.cli.api_client import ApiClient

_TOKEN = "SENTINEL-API-TOKEN-DO-NOT-LEAK"
_PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
_CSV_BODY = b"symbol,value\nBTCUSDT,1\n"
_JSON_BODY = b'{"ok": true, "data": {}}'


class _Handler(BaseHTTPRequestHandler):
    server: _RecordingServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        authorization = self.headers.get("Authorization")
        self.server.requests.append(
            {
                "path": self.path,
                "host": self.headers.get("Host"),
                "authorization": authorization,
            }
        )
        body = _CSV_BODY if "/export" in self.path else _JSON_BODY
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

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
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


def _hostile_proxy_env(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    for name in _PROXY_ENV:
        monkeypatch.setenv(name, proxy_url)
    # A bypass list would mask the leak instead of proving it.
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)


def test_both_request_paths_bypass_a_hostile_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _running(_RecordingServer()) as daemon, _running(_RecordingServer()) as hostile:
        _hostile_proxy_env(monkeypatch, hostile.url)
        # Force urllib to rebuild its default opener from the hostile environment
        # so the unpatched client demonstrably consults the proxy.
        monkeypatch.setattr(urllib.request, "_opener", None)

        client = ApiClient(daemon.url, _TOKEN)

        # JSON path.
        assert client.list_scans(limit=1) == {"ok": True, "data": {}}
        # Raw-bytes path (``export_report`` -> ``_request_bytes``).
        body, _headers = client.export_report("RPT-1", export_format="csv")

        assert body == _CSV_BODY
        # Nothing reached the proxy, so the bearer token never left loopback.
        assert hostile.requests == []
        assert [request["path"] for request in daemon.requests] == [
            "/api/v1/scans?limit=1",
            "/api/v1/reports/RPT-1/export?format=csv",
        ]
        assert all(request["authorization"] == f"Bearer {_TOKEN}" for request in daemon.requests)
