"""SEC-01: the Telegram adapter must not consult ambient proxy settings."""

from __future__ import annotations

import httpx
import pytest

from oft.adapters.telegram_bot_api import TelegramBotApiAdapter

SENTINEL = "123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv"
_PROXY_ENV = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.mark.parametrize("inject_transport", [False, True])
def test_adapter_client_ignores_hostile_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, *, inject_transport: bool
) -> None:
    for name in _PROXY_ENV:
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    adapter = TelegramBotApiAdapter(
        token_resolver=lambda: SENTINEL,
        transport=transport if inject_transport else None,
    )
    try:
        client = adapter._client_or_create()

        assert client.trust_env is False
        # No environment proxy is installed as a mount.
        assert client._mounts == {}
    finally:
        adapter.close()
