"""Store read failures surface as DATA_UNAVAILABLE instead of an empty registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.config import Settings
from oft.ports.store import StoreUnavailableError


def _corrupt_store(tmp_path: Path) -> SqliteStore:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "oft.db").write_bytes(b"not a sqlite database")
    return SqliteStore(settings)


_READ_CALLS: list[Callable[[SqliteStore], object]] = [
    lambda store: store.list_symbols(),
    lambda store: store.count_symbols(),
    lambda store: store.get_symbol("BTCUSDT"),
]


@pytest.mark.parametrize("read_call", _READ_CALLS)
def test_symbol_reads_raise_store_unavailable_on_sqlite_error(
    tmp_path: Path, read_call: Callable[[SqliteStore], object]
) -> None:
    store = _corrupt_store(tmp_path)

    with pytest.raises(StoreUnavailableError) as excinfo:
        read_call(store)

    assert excinfo.value.code == "DATA_UNAVAILABLE"


def test_api_maps_store_failure_to_data_unavailable(settings: Settings) -> None:
    app = create_app(settings)

    def boom(**_: object) -> object:
        raise StoreUnavailableError("symbol registry read failed: locked")

    app.state.container.symbol_registry.list_symbols = boom  # type: ignore[method-assign]
    headers = {"Authorization": f"Bearer {app.state.api_token}"}

    with TestClient(app) as client:
        response = client.get("/api/v1/symbols", headers=headers)

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "DATA_UNAVAILABLE"
    # The raw sqlite text must stay in the log, never the response.
    assert body["error"]["message"] == "Required data does not exist or cannot be fetched."
    assert "locked" not in response.text


def test_list_manifest_maps_a_corrupt_database(tmp_path: Path) -> None:
    store = _corrupt_store(tmp_path)

    with pytest.raises(StoreUnavailableError):
        store.list_manifest()


def test_list_manifest_maps_an_unreadable_database(tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("chmod 000 is not enforced for root")
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    store.path.chmod(0o000)
    try:
        with pytest.raises(StoreUnavailableError):
            store.list_manifest()
    finally:
        store.path.chmod(0o600)
