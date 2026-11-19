"""Verified SQLite backups: consistent copies, checksums, and snapshots.

This module is deliberately stdlib-only. ``sqlite_schema`` imports it for the
pre-migration snapshot, so it must never import ``oft.adapters.sqlite_schema``
(or ``oft.application``); callers pass explicit paths and instants.

A backup is produced with ``VACUUM INTO`` into a temporary file in the target
directory, verified with ``PRAGMA integrity_check`` (the result must be exactly
``ok``), hashed, given a ``.sha256`` sidecar, and only then renamed into place.
A partial or unverified file is never presented under the final name.

Filename timestamps are UTC ``%Y%m%dT%H%M%S%fZ`` (microseconds avoid collisions).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from oft.config import Settings

BACKUPS_DIRNAME = "backups"
BACKUP_GLOB = "oft-*.db"
PRE_MIGRATION_GLOB = "pre-migration-*.db"
PRE_RESTORE_GLOB = "pre-restore-*.db"
PRE_MIGRATION_KEEP = 10
_SHA256_CHUNK_BYTES = 1024 * 1024
# SQLite errors that mean "the source bytes are not a sound database"; they map
# to an integrity failure rather than a generic I/O failure.
_CORRUPTION_MARKERS = ("malformed", "not a database", "corrupt", "integrity")


class BackupError(RuntimeError):
    """Base class for backup/restore failures (a RuntimeError by design)."""


class BackupNotFoundError(BackupError):
    """The candidate file does not exist."""


class BackupIntegrityError(BackupError):
    """``PRAGMA integrity_check`` did not return exactly ``ok``."""


class BackupHashMismatchError(BackupError):
    """The candidate's sha256 does not match its ``.sha256`` sidecar."""


class BackupIOError(BackupError):
    """A file-system or SQLite I/O operation failed."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackupAgeReport:
    last_backup_at: datetime | None
    age_seconds: float | None
    snapshot_count: int
    total_bytes: int


def filename_timestamp(now: datetime) -> str:
    """UTC filename stamp with microseconds so two backups never collide."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def backups_dir(settings: Settings, *, create: bool = True) -> Path:
    """The private ``<data_dir>/backups`` directory (mode 0700)."""
    directory = settings.data_dir / BACKUPS_DIRNAME
    if create:
        _ensure_private_dir(directory)
    return directory


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def discard(path: Path) -> None:
    """Best-effort removal; cleanup must never mask the original failure."""
    try:
        path.unlink()
    except OSError:
        return


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_SHA256_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_private_dir(directory: Path) -> None:
    """Create an OFT-owned directory and always tighten it to 0700."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def _ensure_dir_exists(directory: Path) -> None:
    """Create missing directories, tightening only the ones this run created.

    A caller-supplied destination (``oft backup --out``) may live in a shared or
    pre-existing directory; its mode must never be mutated. Parents created here
    are private because they are OFT-created, matching ``backups_dir``.
    """
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        try:
            created.chmod(0o700)
        except OSError:
            # Tightening is best-effort; the backup file itself is 0600.
            continue


def _check_integrity(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` and classify any failure."""
    uri = path.resolve().as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    row: tuple | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        message = str(exc).lower()
        if any(marker in message for marker in _CORRUPTION_MARKERS):
            raise BackupIntegrityError(
                f"backup failed PRAGMA integrity_check: {path}: {exc}"
            ) from exc
        # Busy/locked, unopenable, or permission failures say nothing about
        # whether the bytes are a sound database.
        raise BackupIOError(f"cannot read backup {path}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    if not (row is not None and row[0] == "ok"):
        raise BackupIntegrityError(f"backup failed PRAGMA integrity_check: {path}")


def verify_integrity(path: Path) -> None:
    """Raise unless ``PRAGMA integrity_check`` returns exactly ``ok``."""
    if not path.is_file():
        raise BackupNotFoundError(f"backup file does not exist: {path}")
    _check_integrity(path)


def _expected_sha256(path: Path) -> str | None:
    sidecar = sidecar_path(path)
    try:
        text = sidecar.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupIOError(f"cannot read checksum sidecar {sidecar}: {exc}") from exc
    tokens = text.split()
    if not tokens:
        raise BackupIOError(f"checksum sidecar is empty: {sidecar}")
    return tokens[0].lower()


def _write_sidecar(path: Path, digest: str) -> None:
    sidecar = sidecar_path(path)
    temporary = sidecar.with_name(f".{sidecar.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temporary.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        os.replace(temporary, sidecar)
    except OSError as exc:
        discard(temporary)
        raise BackupIOError(f"cannot write checksum sidecar {sidecar}: {exc}") from exc


def validate_candidate(path: Path) -> BackupVerification:
    """Verify a restore candidate's integrity and (when present) checksum."""
    if not path.is_file():
        raise BackupNotFoundError(f"backup file does not exist: {path}")
    verify_integrity(path)
    try:
        digest = sha256_file(path)
        size = path.stat().st_size
    except OSError as exc:
        raise BackupIOError(f"cannot read backup {path}: {exc}") from exc
    expected = _expected_sha256(path)
    if expected is not None and expected != digest:
        raise BackupHashMismatchError(
            f"backup checksum mismatch for {path}: sidecar {expected}, actual {digest}"
        )
    return BackupVerification(path=path, size_bytes=size, sha256=digest)


def _discard_partial(temporary: Path, destination: Path | None) -> None:
    """Remove a failed copy's temp file and any sidecar written for it."""
    discard(temporary)
    if destination is not None:
        discard(sidecar_path(destination))


def _write_verified_copy(db_path: Path, destination: Path) -> tuple[int, str]:
    """``VACUUM INTO`` a temp file, verify it, hash it, and rename it into place."""
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    sidecar_written = False
    try:
        source = sqlite3.connect(str(db_path))
        try:
            source.execute("PRAGMA busy_timeout = 5000")
            source.execute("VACUUM INTO ?", (str(temporary),))
        finally:
            source.close()
        # Tighten the copy before any further reads, so a slow verification
        # never leaves a group/world-readable database on disk.
        temporary.chmod(0o600)
        verify_integrity(temporary)
        digest = sha256_file(temporary)
        size = temporary.stat().st_size
        _write_sidecar(destination, digest)
        sidecar_written = True
        os.replace(temporary, destination)
    except Exception:
        _discard_partial(temporary, destination if sidecar_written else None)
        raise
    return size, digest


def create_consistent_copy(
    db_path: Path,
    destination: Path,
    *,
    now: datetime,
) -> BackupResult:
    """Snapshot ``db_path`` into ``destination`` via a verified ``VACUUM INTO``.

    The copy is written under a temporary name in the destination directory,
    integrity-checked, hashed, given a sidecar, and only then renamed into
    place. A failure leaves no file under ``destination``. ``now`` is accepted
    so callers share one injected instant for the destination name.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BackupNotFoundError(f"database does not exist: {db_path}")
    destination = Path(destination)
    try:
        _ensure_dir_exists(destination.parent)
        size, digest = _write_verified_copy(db_path, destination)
    except BackupError:
        raise
    except sqlite3.Error as exc:
        if any(marker in str(exc).lower() for marker in _CORRUPTION_MARKERS):
            raise BackupIntegrityError(
                f"source database {db_path} failed while copying: {exc}"
            ) from exc
        raise BackupIOError(f"cannot create backup {destination}: {exc}") from exc
    except OSError as exc:
        raise BackupIOError(f"cannot create backup {destination}: {exc}") from exc
    return BackupResult(path=destination, size_bytes=size, sha256=digest)


def _snapshot_path(directory: Path, prefix: str, now: datetime) -> Path:
    return directory / f"{prefix}-{filename_timestamp(now)}.db"


def snapshot_before_migration(
    db_path: Path,
    schema_version: int,
    *,
    now: datetime,
) -> Path:
    """Write ``pre-migration-<version>-<ts>.db`` and prune old snapshots."""
    directory = db_path.parent / BACKUPS_DIRNAME
    destination = _snapshot_path(directory, f"pre-migration-{schema_version}", now)
    try:
        result = create_consistent_copy(db_path, destination, now=now)
    except BackupError as exc:
        raise BackupError(f"pre-migration snapshot failed for {db_path}: {exc}") from exc
    _prune_snapshots(directory, PRE_MIGRATION_GLOB, keep=PRE_MIGRATION_KEEP)
    return result.path


def snapshot_before_restore(db_path: Path, *, now: datetime) -> Path:
    """Write ``pre-restore-<ts>.db`` before ``oft restore`` swaps the database."""
    directory = db_path.parent / BACKUPS_DIRNAME
    destination = _snapshot_path(directory, "pre-restore", now)
    try:
        return create_consistent_copy(db_path, destination, now=now).path
    except BackupError as exc:
        raise BackupError(f"pre-restore safety snapshot failed for {db_path}: {exc}") from exc


def _prune_snapshots(directory: Path, pattern: str, *, keep: int) -> None:
    """Best-effort bound on old snapshots; never fails a completed snapshot."""
    try:
        snapshots = list(directory.glob(pattern))
    except OSError:
        return
    if len(snapshots) <= keep:
        return
    ordered: list[tuple[int, str, Path]] = []
    for path in snapshots:
        try:
            ordered.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    ordered.sort()
    for _stamp, _name, stale in ordered[:-keep]:
        discard(stale)
        discard(sidecar_path(stale))


def backup_age_report(settings: Settings, *, now: datetime) -> BackupAgeReport:
    """Summarise the backup directory for the doctor check (never creates it)."""
    directory = settings.data_dir / BACKUPS_DIRNAME
    total_bytes = 0
    snapshot_count = 0
    last_backup_at: datetime | None = None
    if directory.is_dir():
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise BackupIOError(f"cannot list backups in {directory}: {exc}") from exc
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                continue
        snapshot_count = len(
            [*directory.glob(PRE_MIGRATION_GLOB), *directory.glob(PRE_RESTORE_GLOB)]
        )
        newest_stamp: int | None = None
        for path in directory.glob(BACKUP_GLOB):
            if not path.is_file():
                continue
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            if newest_stamp is None or stamp > newest_stamp:
                newest_stamp = stamp
        if newest_stamp is not None:
            last_backup_at = datetime.fromtimestamp(newest_stamp / 1_000_000_000, UTC)
    age: float | None = None
    if last_backup_at is not None:
        age = max(0.0, (now.astimezone(UTC) - last_backup_at).total_seconds())
    return BackupAgeReport(
        last_backup_at=last_backup_at,
        age_seconds=age,
        snapshot_count=snapshot_count,
        total_bytes=total_bytes,
    )


def format_size(size_bytes: int) -> str:
    """Compact human size for operator-facing diagnostics."""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
