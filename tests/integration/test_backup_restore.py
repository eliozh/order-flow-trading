"""Integration: ``oft backup`` / ``oft restore``, pre-migration snapshots, doctor.

These tests cover slice 57 B1-B4 end to end in-process against a temporary data
directory:

* B1: ``oft backup`` writes a verified ``VACUUM INTO`` copy plus a sha256
  sidecar and reports it through the shared JSON envelope.
* B2: ``oft restore`` validates the candidate, refuses while a daemon runs,
  requires ``--yes``, takes a ``pre-restore`` safety snapshot, and atomically
  swaps the database.
* B3: ``initialize_database`` snapshots an existing database before a pending
  migration and keeps at most ten pre-migration snapshots.
* B4: ``oft doctor`` reports the last successful backup age, snapshot count,
  and backup directory size (``never`` when none exists).

The suite is CI-hermetic: no network, no real daemon, no keyring, and no
``OFT_MASTER_KEY``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from oft.adapters import sqlite_backup as backup_adapter
from oft.adapters import sqlite_schema
from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.parquet_store import ParquetStore
from oft.adapters.sqlite_backup import (
    BackupIntegrityError,
    BackupIOError,
    validate_candidate,
)
from oft.adapters.sqlite_schema import _SCHEMA_VERSION, initialize_database
from oft.adapters.sqlite_store import SqliteStore
from oft.application.health import DaemonProbe, build_doctor
from oft.cli.commands import backup as backup_module
from oft.cli.main import app
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

_SECRET_ENV = (
    "OFT_MASTER_KEY",
    "OFT_API_TOKEN",
    "OFT_OPENAI_API_KEY",
    "OFT_ANTHROPIC_API_KEY",
    "OFT_DEEPSEEK_API_KEY",
    "OFT_TELEGRAM_BOT_TOKEN",
)

# A real v1 database shape: enough for the version-2 symbols migration and every
# later additive migration.
_V1_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-11-06T00:00:00.000Z');
CREATE TABLE symbols (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    underlying_type TEXT,
    status TEXT NOT NULL,
    tick_size TEXT NOT NULL,
    step_size TEXT NOT NULL,
    min_qty TEXT NOT NULL,
    min_notional TEXT NOT NULL,
    price_precision INTEGER NOT NULL,
    quantity_precision INTEGER NOT NULL,
    onboard_date INTEGER NOT NULL,
    tradifi INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO symbols VALUES (
    'BTCUSDT', 'BTC', 'USDT', 'PERPETUAL', 'COIN', 'TRADING',
    '0.10', '0.001', '0.001', '5.00', 2, 3, 1567900800000, 0,
    '2026-11-06T04:00:00.000Z'
);
"""


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Never read ambient shell config, a real keyring, or a real master key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFT_ENV", "test")
    monkeypatch.setenv("OFT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    for name in _SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    # Never probe (or trust) a developer's real daemon: the refusal test
    # overrides this seam explicitly.
    monkeypatch.setattr(backup_module, "_daemon_running", lambda _settings: False)
    yield


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", data_dir=tmp_path / "data")


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


def _create_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _update_db(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE payload SET value = ?", (value,))
        connection.commit()
    finally:
        connection.close()


def _read_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM payload").fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return bool(row is not None and row[0] == "ok")


def _envelope(result: Result) -> dict[str, object]:
    return json.loads(result.stdout)


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


# ---------------------------------------------------------------------------
# B1/B2 — backup round trip
# ---------------------------------------------------------------------------


def test_backup_round_trip_restores_content_and_writes_safety_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "before")

    backed_up = _invoke("backup", "--json")

    assert backed_up.exit_code == 0, backed_up.stderr
    envelope = _envelope(backed_up)
    assert envelope["ok"] is True
    data = envelope["data"]
    backup_path = Path(data["path"])
    assert backup_path.parent == settings.data_dir / "backups"
    assert backup_path.name.startswith("oft-")
    assert backup_path.suffix == ".db"
    assert backup_path.is_file()
    assert _integrity_ok(backup_path)
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert data["sha256"] == digest
    assert data["size_bytes"] == backup_path.stat().st_size
    assert _sidecar(backup_path).is_file()
    assert _sidecar(backup_path).read_text(encoding="utf-8").split()[0] == digest

    # Mutating the live database after the backup must not affect the copy.
    _update_db(db_path, "after")

    # Without --yes, JSON mode refuses with a validation failure and changes
    # nothing.
    refused = _invoke("restore", str(backup_path), "--json")
    assert refused.exit_code == 1
    assert _envelope(refused)["error"]["code"] == "VALIDATION_FAILED"
    assert _read_value(db_path) == "after"

    restored = _invoke("restore", str(backup_path), "--yes", "--json")

    assert restored.exit_code == 0, restored.stderr
    restored_data = _envelope(restored)["data"]
    assert Path(restored_data["restored"]) == db_path
    assert Path(restored_data["source"]) == backup_path
    assert _read_value(db_path) == "before"
    assert _integrity_ok(db_path)

    safety_path = Path(restored_data["safety_snapshot"])
    assert safety_path.is_file()
    assert safety_path.parent == settings.data_dir / "backups"
    assert safety_path.name.startswith("pre-restore-")
    # The safety snapshot preserves the pre-restore (mutated) state.
    assert _read_value(safety_path) == "after"
    assert _integrity_ok(safety_path)


# ---------------------------------------------------------------------------
# B1 — --out directory handling and failed copies
# ---------------------------------------------------------------------------


def test_backup_preserves_the_mode_of_an_existing_out_directory(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _create_db(settings.data_dir / "oft.db", "value")
    out = tmp_path / "user-backups"
    out.mkdir()
    out.chmod(0o755)

    result = _invoke("backup", "--out", str(out), "--json")

    assert result.exit_code == 0, result.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o755
    backup_path = Path(_envelope(result)["data"]["path"])
    assert backup_path.parent == out
    assert backup_path.is_file()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600


def test_backup_creates_a_missing_out_directory_with_private_mode(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _create_db(settings.data_dir / "oft.db", "value")
    out = tmp_path / "new-backups"

    result = _invoke("backup", "--out", str(out), "--json")

    assert result.exit_code == 0, result.stderr
    assert out.is_dir()
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_failed_backup_leaves_no_partial_file_or_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _create_db(settings.data_dir / "oft.db", "value")

    def fail_verification(_path: Path) -> None:
        raise BackupIntegrityError("simulated verification failure")

    monkeypatch.setattr(backup_adapter, "verify_integrity", fail_verification)

    result = _invoke("backup", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "BACKUP_INTEGRITY_FAILED"
    backups = settings.data_dir / "backups"
    assert list(backups.iterdir()) == []


# ---------------------------------------------------------------------------
# B2 — rejected candidates
# ---------------------------------------------------------------------------


def test_restore_rejects_a_corrupt_candidate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database" * 64)

    result = _invoke("restore", str(corrupt), "--yes", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "BACKUP_INTEGRITY_FAILED"
    assert _read_value(db_path) == "keep"


def test_unreadable_candidate_is_a_backup_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "value")

    def broken_connect(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", broken_connect)

    with pytest.raises(BackupIOError):
        validate_candidate(candidate)


def test_restore_rejects_a_hash_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _create_db(settings.data_dir / "oft.db", "keep")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    _sidecar(candidate).write_text("0" * 64 + "  candidate.db\n", encoding="utf-8")

    result = _invoke("restore", str(candidate), "--yes", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "BACKUP_HASH_MISMATCH"


def test_restore_rejects_a_candidate_changed_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    # A different *valid* database stands in for a same-user replacement between
    # validate and install: integrity still passes, only the digest differs.
    impostor = tmp_path / "impostor.db"
    _create_db(impostor, "other")
    real_copy = backup_module._copy_candidate

    def corrupting_copy(source: Path, data_dir: Path) -> Path:
        temporary = real_copy(source, data_dir)
        shutil.copyfile(impostor, temporary)
        return temporary

    monkeypatch.setattr(backup_module, "_copy_candidate", corrupting_copy)

    result = _invoke("restore", str(candidate), "--yes", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "BACKUP_HASH_MISMATCH"
    assert _read_value(db_path) == "keep"
    assert [path for path in settings.data_dir.iterdir() if ".restore-" in path.name] == []


def test_restore_refuses_while_the_daemon_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    monkeypatch.setattr(backup_module, "_daemon_running", lambda _settings: True)

    result = _invoke("restore", str(candidate), "--yes", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "DAEMON_RUNNING"
    assert _read_value(db_path) == "keep"


def test_restore_rechecks_the_daemon_immediately_before_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    probes = iter((False, True))
    monkeypatch.setattr(backup_module, "_daemon_running", lambda _settings: next(probes))

    result = _invoke("restore", str(candidate), "--yes", "--json")

    assert result.exit_code == 1
    assert _envelope(result)["error"]["code"] == "DAEMON_RUNNING"
    assert _read_value(db_path) == "keep"
    assert [path for path in settings.data_dir.iterdir() if ".restore-" in path.name] == []


def test_restore_rejects_a_candidate_with_journal_siblings(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    wal = candidate.with_name(candidate.name + "-wal")
    wal.write_bytes(b"unflushed wal")

    result = _invoke("restore", str(candidate), "--yes", "--json")

    assert result.exit_code == 1
    error = _envelope(result)["error"]
    assert error["code"] == "BACKUP_IO_ERROR"
    assert str(wal) in error["message"]
    assert _read_value(db_path) == "keep"


def test_restore_no_snapshot_replaces_a_corrupt_live_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    settings.data_dir.mkdir(parents=True)
    db_path.write_bytes(b"this is not a sqlite database" * 8)
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")

    result = _invoke("restore", str(candidate), "--yes", "--no-snapshot", "--json")

    assert result.exit_code == 0, result.stderr
    data = _envelope(result)["data"]
    assert data["safety_snapshot"] is None
    assert data["snapshot_skipped"] is True
    assert "--no-snapshot" in result.stderr
    assert _read_value(db_path) == "restored"
    assert list((settings.data_dir / "backups").glob("pre-restore-*.db")) == []


def test_restore_names_stranded_journals_when_the_put_back_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "keep")
    journal = db_path.with_name(db_path.name + "-wal")
    journal.write_bytes(b"live wal")
    candidate = tmp_path / "candidate.db"
    _create_db(candidate, "restored")
    real_replace = os.replace

    def failing_replace(source: object, destination: object) -> None:
        if Path(str(destination)) == db_path:
            raise OSError("simulated swap failure")
        if ".restore-bak-" in Path(str(source)).name:
            raise OSError("simulated put-back failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)

    # ``--no-snapshot`` keeps the stray ``-wal`` file in place: taking a
    # pre-restore snapshot would open the live database and remove it.
    result = _invoke("restore", str(candidate), "--yes", "--no-snapshot", "--json")

    assert result.exit_code == 1
    error = _envelope(result)["error"]
    assert error["code"] == "BACKUP_IO_ERROR"
    assert ".restore-bak-" in error["message"]
    assert _read_value(db_path) == "keep"


def test_snapshot_helpers_preserve_the_backup_error_subclass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "oft.db"
    _create_db(db_path, "value")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise BackupIntegrityError("simulated verification failure")

    monkeypatch.setattr(backup_adapter, "create_consistent_copy", fail)

    with pytest.raises(BackupIntegrityError):
        backup_adapter.snapshot_before_migration(db_path, 2, now=NOW)
    with pytest.raises(BackupIntegrityError):
        backup_adapter.snapshot_before_restore(db_path, now=NOW)


# ---------------------------------------------------------------------------
# Durability and permissions — publication ordering
# ---------------------------------------------------------------------------


def test_publication_fsyncs_files_before_rename_and_directories_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "value")

    events: list[tuple[str, Path, Path | None]] = []
    real_fsync_file = backup_adapter.fsync_file
    real_fsync_dir = backup_adapter.fsync_dir
    real_replace = os.replace

    def spy_fsync_file(path: Path) -> None:
        events.append(("fsync_file", Path(path), None))
        real_fsync_file(path)

    def spy_fsync_dir(path: Path) -> None:
        events.append(("fsync_dir", Path(path), None))
        real_fsync_dir(path)

    def spy_replace(source: object, destination: object) -> None:
        events.append(("replace", Path(str(source)), Path(str(destination))))
        real_replace(source, destination)

    monkeypatch.setattr(backup_adapter, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(backup_adapter, "fsync_dir", spy_fsync_dir)
    monkeypatch.setattr(backup_module, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(backup_module, "fsync_dir", spy_fsync_dir)
    monkeypatch.setattr(os, "replace", spy_replace)

    def replacements_for(destination: Path) -> list[int]:
        return [
            index
            for index, (kind, _source, dest) in enumerate(events)
            if kind == "replace" and dest == destination
        ]

    def assert_parent_fsync_after(index: int, parent: Path) -> None:
        assert any(
            kind == "fsync_dir" and path == parent for kind, path, _source in events[index + 1 :]
        )

    backed_up = _invoke("backup", "--json")
    assert backed_up.exit_code == 0, backed_up.stderr
    backup_path = Path(_envelope(backed_up)["data"]["path"])
    sidecar = _sidecar(backup_path)

    db_rename = replacements_for(backup_path)
    assert len(db_rename) == 1
    temporary_db = events[db_rename[0]][1]
    assert any(
        kind == "fsync_file" and path == temporary_db
        for kind, path, _source in events[: db_rename[0]]
    )
    assert_parent_fsync_after(db_rename[0], backup_path.parent)

    sidecar_rename = replacements_for(sidecar)
    assert len(sidecar_rename) == 1
    temporary_sidecar = events[sidecar_rename[0]][1]
    assert any(
        kind == "fsync_file" and path == temporary_sidecar
        for kind, path, _source in events[: sidecar_rename[0]]
    )
    assert_parent_fsync_after(sidecar_rename[0], sidecar.parent)

    events.clear()
    restored = _invoke("restore", str(backup_path), "--yes", "--json")
    assert restored.exit_code == 0, restored.stderr
    swap_rename = replacements_for(db_path)
    assert len(swap_rename) == 1
    temporary_restore = events[swap_rename[0]][1]
    assert any(
        kind == "fsync_file" and path == temporary_restore
        for kind, path, _source in events[: swap_rename[0]]
    )
    assert any(
        kind == "fsync_dir" and path == db_path.parent
        for kind, path, _source in events[swap_rename[0] + 1 :]
    )


def test_vacuum_copy_is_created_under_a_private_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _create_db(settings.data_dir / "oft.db", "value")
    real_umask = os.umask
    calls: list[int] = []
    original_umask = real_umask(0o022)

    def spy_umask(mask: int) -> int:
        calls.append(mask)
        return real_umask(mask)

    monkeypatch.setattr(backup_adapter.os, "umask", spy_umask)
    try:
        result = _invoke("backup", "--json")
    finally:
        real_umask(original_umask)

    assert result.exit_code == 0, result.stderr
    assert calls[0] == 0o077
    assert calls[-1] == 0o022


# ---------------------------------------------------------------------------
# B3 — pre-migration snapshot
# ---------------------------------------------------------------------------


def test_initialize_database_snapshots_a_pending_migration(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    db_path = settings.data_dir / "oft.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_V1_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    initialize_database(settings)

    snapshots = sorted((settings.data_dir / "backups").glob("pre-migration-1-*.db"))
    assert len(snapshots) == 1
    assert _integrity_ok(snapshots[0])
    snapshot = sqlite3.connect(snapshots[0])
    try:
        version = snapshot.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        snapshot.close()
    assert version == 1
    assert _sidecar(snapshots[0]).is_file()

    upgraded = sqlite3.connect(db_path)
    try:
        current = upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        upgraded.close()
    assert current == _SCHEMA_VERSION


def test_a_failed_pre_migration_snapshot_aborts_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    db_path = settings.data_dir / "oft.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_V1_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated snapshot failure")

    # ``initialize_database`` looks the helper up on this module.
    monkeypatch.setattr(sqlite_schema, "snapshot_before_migration", boom)

    with pytest.raises(RuntimeError):
        initialize_database(settings)

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        symbols_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'symbols'"
        ).fetchone()[0]
    finally:
        connection.close()
    # No pending migration ran: the version and the v1 table shape are intact.
    assert version == 1
    assert "CHECK" not in symbols_ddl


def test_pre_migration_snapshots_are_bounded_to_ten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "current")
    backups = settings.data_dir / "backups"
    backups.mkdir(parents=True)
    base = datetime(2020, 1, 1, tzinfo=UTC)
    for index in range(12):
        old = backups / f"pre-migration-1-2020010{index}T000000000000Z.db"
        old.write_bytes(b"old snapshot")
        _sidecar(old).write_text("0" * 64 + "  old\n", encoding="utf-8")
        stamp = (base + timedelta(days=index)).timestamp()
        os.utime(old, (stamp, stamp))

    from oft.adapters.sqlite_backup import snapshot_before_migration

    newest = snapshot_before_migration(db_path, 2, now=NOW)

    remaining = sorted(backups.glob("pre-migration-*.db"))
    assert len(remaining) == 10
    assert newest in remaining
    assert len(list(backups.glob("*.sha256"))) == 10
    assert all(_sidecar(path).is_file() for path in remaining)


def test_pre_restore_snapshots_are_bounded_to_ten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.data_dir / "oft.db"
    _create_db(db_path, "current")
    backups = settings.data_dir / "backups"
    backups.mkdir(parents=True)
    base = datetime(2020, 1, 1, tzinfo=UTC)
    for index in range(12):
        old = backups / f"pre-restore-2020010{index}T000000000000Z.db"
        old.write_bytes(b"old snapshot")
        _sidecar(old).write_text("0" * 64 + "  old\n", encoding="utf-8")
        stamp = (base + timedelta(days=index)).timestamp()
        os.utime(old, (stamp, stamp))

    from oft.adapters.sqlite_backup import snapshot_before_restore

    newest = snapshot_before_restore(db_path, now=NOW)

    remaining = sorted(backups.glob("pre-restore-*.db"))
    assert len(remaining) == 10
    assert newest in remaining
    assert len(list(backups.glob("*.sha256"))) == 10
    assert all(_sidecar(path).is_file() for path in remaining)


def test_initialize_database_snapshots_an_idempotent_repair(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize_database(settings)
    db_path = settings.data_dir / "oft.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.commit()
    finally:
        connection.close()

    initialize_database(settings)

    snapshots = sorted((settings.data_dir / "backups").glob("pre-migration-30-*.db"))
    assert len(snapshots) == 1
    snapshot = sqlite3.connect(snapshots[0])
    try:
        version = snapshot.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        snapshot.close()
    assert version == 30


def test_initialize_database_skips_the_snapshot_when_nothing_will_change(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    initialize_database(settings)
    initialize_database(settings)

    assert list((settings.data_dir / "backups").glob("pre-migration-*.db")) == []


def _insert_opportunity_with_null_expiry(db_path: Path, payload_json: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO opportunities (
                opportunity_id, symbol, state, created_at, updated_at,
                payload_json, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "opp-null-expiry",
                "BTCUSDT",
                "OPEN",
                "2026-11-19T00:00:00.000Z",
                "2026-11-19T00:00:00.000Z",
                payload_json,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_initialize_database_does_not_snapshot_an_unrepairable_null_expiry(
    tmp_path: Path,
) -> None:
    """A NULL expires_at the repair skips must not trigger a snapshot forever."""
    settings = _settings(tmp_path)
    initialize_database(settings)
    db_path = settings.data_dir / "oft.db"
    _insert_opportunity_with_null_expiry(db_path, '{"no_expiry": true}')

    initialize_database(settings)

    assert list((settings.data_dir / "backups").glob("pre-migration-*.db")) == []


def test_initialize_database_snapshots_a_repairable_null_expiry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize_database(settings)
    db_path = settings.data_dir / "oft.db"
    _insert_opportunity_with_null_expiry(db_path, '{"expires_at": "2026-11-19T12:00:00+00:00"}')

    initialize_database(settings)

    snapshots = sorted((settings.data_dir / "backups").glob("pre-migration-30-*.db"))
    assert len(snapshots) == 1


# ---------------------------------------------------------------------------
# B4 — doctor reports backup age
# ---------------------------------------------------------------------------


def _doctor(settings: Settings) -> dict[str, object]:
    store = SqliteStore(settings)
    store.initialize()
    ParquetStore(settings).ensure_layout()
    report = build_doctor(
        settings,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    return {check.name: check for check in report.checks}


def _write_backup(settings: Settings, *, age: timedelta, name: str) -> Path:
    backups = settings.data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / name
    path.write_bytes(b"backup")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_doctor_reports_never_when_no_backup_exists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    check = _doctor(settings)["backup"]

    assert check.status == "degraded"
    assert "never" in check.detail
    assert "snapshots=0" in check.detail


def test_doctor_reports_a_recent_backup_as_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_backup(settings, age=timedelta(hours=2), name="oft-20261119T100000000000Z.db")
    snapshots = settings.data_dir / "backups"
    (snapshots / "pre-migration-29-20261119T090000000000Z.db").write_bytes(b"snapshot")

    check = _doctor(settings)["backup"]

    assert check.status == "ok"
    assert "2.0h" in check.detail
    assert "snapshots=1" in check.detail
    assert "backups=" in check.detail


def test_doctor_flags_a_stale_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_backup(settings, age=timedelta(hours=50), name="oft-20261117T100000000000Z.db")

    check = _doctor(settings)["backup"]

    assert check.status == "degraded"
    assert "50.0h" in check.detail


def test_backup_age_report_counts_only_backup_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    backups = settings.data_dir / "backups"
    backups.mkdir(parents=True)
    backup_path = backups / "oft-20261119T100000000000Z.db"
    backup_path.write_bytes(b"backup")
    _sidecar(backup_path).write_text("0" * 64 + "  backup\n", encoding="utf-8")
    (backups / "pre-migration-30-20261119T090000000000Z.db").write_bytes(b"snapshot")
    (backups / "unrelated.bin").write_bytes(b"x" * 100)

    report = backup_adapter.backup_age_report(settings, now=NOW)

    assert report.total_bytes == len(b"backup")
    assert report.snapshot_count == 1
