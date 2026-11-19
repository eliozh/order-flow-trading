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
from typer.testing import CliRunner

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


def _invoke(*args: str) -> object:
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


def _envelope(result: object) -> dict:
    return json.loads(result.stdout)  # type: ignore[attr-defined]


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
