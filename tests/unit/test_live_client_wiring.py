"""The daemon composition root must not build unlimited live clients."""

from __future__ import annotations

from pathlib import Path

from oft.bootstrap import build_container
from oft.cli.main import _build_limited_clients
from oft.config import Settings


def test_live_clients_share_limiter_and_ws_budget(tmp_path: Path) -> None:
    container = build_container(
        Settings(env="test", port=8765, data_dir=tmp_path / "data"), initialize=True
    )

    ws_client, rest_client = _build_limited_clients(container)

    assert ws_client._subscription_budget is not None
    assert rest_client._rate_limiter is not None
    assert rest_client._rate_limiter._clock is container.clock
