"""Store read failures surface as DATA_UNAVAILABLE instead of an empty registry."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oft.adapters import sqlite_store as sqlite_store_module
from oft.adapters.sqlite_store import SqliteStore
from oft.api.app import create_app
from oft.config import ConfigError, Settings
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


def _patched_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> SqliteStore:
    """A store whose initialization raises ``error`` before touching SQLite."""
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)

    def _raise(_settings: Settings) -> None:
        raise error

    monkeypatch.setattr(sqlite_store_module, "initialize_database", _raise)
    return store


def test_initialize_maps_a_corrupt_database_to_store_unavailable(tmp_path: Path) -> None:
    store = _corrupt_store(tmp_path)

    with pytest.raises(StoreUnavailableError) as excinfo:
        store.initialize()

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "unreadable" in excinfo.value.message
    assert "move or repair" in excinfo.value.message
    assert "oft doctor" in excinfo.value.message
    assert str(store.path) in excinfo.value.message


@pytest.mark.parametrize("message", ["database is locked", "database is busy", "table is locked"])
def test_initialize_maps_a_locked_database_to_a_busy_store_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, message: str
) -> None:
    store = _patched_store(monkeypatch, tmp_path, sqlite3.OperationalError(message))

    with pytest.raises(StoreUnavailableError) as excinfo:
        store.initialize()

    assert excinfo.value.code == "DATA_UNAVAILABLE"
    assert "busy" in excinfo.value.message
    assert "oft serve" in excinfo.value.message
    assert "stop" in excinfo.value.message
    assert "unreadable" not in excinfo.value.message
    assert message not in excinfo.value.message


@pytest.mark.parametrize(
    "message",
    [
        "unable to open database file",
        "attempt to write a readonly database",
        "database or disk is full",
    ],
)
def test_initialize_maps_permission_and_space_errors_to_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, message: str
) -> None:
    store = _patched_store(monkeypatch, tmp_path, sqlite3.OperationalError(message))

    with pytest.raises(ConfigError) as excinfo:
        store.initialize()

    assert excinfo.value.code == "CONFIG_INVALID"
    assert "permissions" in excinfo.value.message
    assert "free disk space" in excinfo.value.message
    assert "oft doctor" in excinfo.value.message
    assert message not in excinfo.value.message


def test_initialize_propagates_a_programming_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = sqlite3.ProgrammingError("cannot operate on a closed database")
    store = _patched_store(monkeypatch, tmp_path, error)

    with pytest.raises(sqlite3.ProgrammingError) as excinfo:
        store.initialize()

    assert excinfo.value is error


def test_initialize_reraises_an_integrity_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = sqlite3.IntegrityError("CHECK constraint failed")
    store = _patched_store(monkeypatch, tmp_path, error)

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        store.initialize()

    assert excinfo.value is error
