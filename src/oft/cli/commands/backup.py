"""``oft backup`` and ``oft restore``: verified database backups.

Reason codes (catalog: ``docs/contracts/error-catalog.json``):

* ``DATA_UNAVAILABLE`` — no database exists to back up.
* ``BACKUP_NOT_FOUND`` — the restore candidate does not exist.
* ``BACKUP_INTEGRITY_FAILED`` — the candidate (or the new copy) failed
  ``PRAGMA integrity_check``.
* ``BACKUP_HASH_MISMATCH`` — a ``.sha256`` sidecar exists but does not match.
* ``BACKUP_IO_ERROR`` — a file-system or SQLite I/O operation failed.
* ``DAEMON_RUNNING`` — restore refuses to swap the database under a live daemon.
* ``VALIDATION_FAILED`` — ``--json`` restore was attempted without ``--yes``.

Backups are files plus checksums only: table rows are never printed, and the
keyring is never copied. Every step writes under a temporary name and renames
only after the copy passes integrity verification.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from oft.adapters.sqlite_backup import (
    BackupError,
    BackupHashMismatchError,
    BackupIntegrityError,
    BackupNotFoundError,
    BackupResult,
    BackupVerification,
    backups_dir,
    create_consistent_copy,
    discard,
    filename_timestamp,
    sha256_file,
    snapshot_before_restore,
    validate_candidate,
    verify_integrity,
)
from oft.adapters.sqlite_schema import database_path
from oft.api.envelope import new_request_id, success_envelope
from oft.application.health import probe_daemon
from oft.cli.commands.daemon import pid_matches, read_pid, settings_or_exit
from oft.cli.commands.doctor import emit_error
from oft.config import Settings

_DAEMON_RUNNING_MESSAGE = (
    "the daemon is running; stop it with `oft daemon stop` before restoring the database"
)
_JOURNAL_SUFFIXES = ("-wal", "-shm", "-journal")


def _daemon_running(settings: Settings) -> bool:
    """Monkeypatchable seam: a healthy probe or a live fenced PID file."""
    if probe_daemon(settings).state == "UP":
        return True
    pid = read_pid(settings)
    return pid is not None and pid_matches(settings, pid)


def _code_for(exc: BackupError) -> str:
    if isinstance(exc, BackupNotFoundError):
        return "BACKUP_NOT_FOUND"
    if isinstance(exc, BackupHashMismatchError):
        return "BACKUP_HASH_MISMATCH"
    if isinstance(exc, BackupIntegrityError):
        return "BACKUP_INTEGRITY_FAILED"
    return "BACKUP_IO_ERROR"


def _emit_failure(exc: BackupError, *, json_output: bool, exit_code: int = 1) -> NoReturn:
    emit_error(_code_for(exc), str(exc), json_output=json_output)
    raise typer.Exit(code=exit_code)


def _emit_os_failure(message: str, *, json_output: bool, exit_code: int = 1) -> NoReturn:
    emit_error("BACKUP_IO_ERROR", message, json_output=json_output)
    raise typer.Exit(code=exit_code)


def _prepare_backup_directory(out: Path | None, settings: Settings, *, json_output: bool) -> Path:
    """Pick or create the backup directory, never re-moding an existing one."""
    if out is None:
        try:
            return backups_dir(settings)
        except OSError as exc:
            _emit_os_failure(
                f"cannot create backup directory {settings.data_dir / 'backups'}: {exc}",
                json_output=json_output,
            )
    directory = out.expanduser()
    # Only a directory this invocation creates is ours to tighten; an existing
    # ``--out`` (shared mount, /tmp, ...) keeps its mode unchanged.
    existed = directory.is_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed:
            directory.chmod(0o700)
    except OSError as exc:
        _emit_os_failure(
            f"cannot create backup directory {directory}: {exc}",
            json_output=json_output,
        )
    return directory


def _emit_backup_success(result: BackupResult, *, duration_ms: int, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    {
                        "path": str(result.path),
                        "size_bytes": result.size_bytes,
                        "sha256": result.sha256,
                        "duration_ms": duration_ms,
                    },
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    typer.echo(f"backup   {result.path}")
    typer.echo(f"size     {result.size_bytes} bytes")
    typer.echo(f"sha256   {result.sha256}")
    typer.echo(f"duration {duration_ms} ms")


def backup_command(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory for the backup (default: <data>/backups)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the shared JSON envelope."),
    ] = False,
) -> None:
    """Write a verified, checksummed copy of the local SQLite database."""
    settings = settings_or_exit(json_output)
    db_path = database_path(settings)
    if not db_path.is_file():
        emit_error(
            "DATA_UNAVAILABLE",
            f"no database to back up at {db_path}; run `oft init` first",
            json_output=json_output,
        )
        raise typer.Exit(code=1)

    directory = _prepare_backup_directory(out, settings, json_output=json_output)
    now = datetime.now(UTC)
    destination = directory / f"oft-{filename_timestamp(now)}.db"
    started = time.monotonic()
    try:
        result = create_consistent_copy(db_path, destination, now=now)
    except BackupError as exc:
        _emit_failure(exc, json_output=json_output)
    duration_ms = int((time.monotonic() - started) * 1000)
    _emit_backup_success(result, duration_ms=duration_ms, json_output=json_output)


def _copy_candidate(candidate: Path, data_dir: Path) -> Path:
    """Copy the candidate into the data dir and fsync the temporary file."""
    temporary = data_dir / f".oft.db.restore-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        shutil.copyfile(candidate, temporary)
        os.chmod(temporary, 0o600)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        discard(temporary)
        raise
    return temporary


def _move_journals_aside(db_path: Path) -> list[tuple[Path, Path]]:
    """Move live SQLite journals aside so they cannot replay onto a restored DB."""
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in _JOURNAL_SUFFIXES:
            journal = db_path.with_name(db_path.name + suffix)
            if not journal.exists():
                continue
            backup = db_path.with_name(
                f"{journal.name}.restore-bak-{os.getpid()}-{os.urandom(4).hex()}"
            )
            os.replace(journal, backup)
            moved.append((journal, backup))
    except OSError:
        _restore_journals(moved)
        raise
    return moved


def _restore_journals(moved: list[tuple[Path, Path]]) -> None:
    """Put moved-aside journals back after a failed swap; best effort."""
    for original, backup in moved:
        try:
            os.replace(backup, original)
        except OSError:
            continue


def _discard_journals(moved: list[tuple[Path, Path]]) -> None:
    """Delete the moved-aside journals after a successful swap."""
    for _original, backup in moved:
        discard(backup)


def _swap_database(db_path: Path, temporary: Path) -> None:
    """Atomically install ``temporary``; a failed replace restores the journals."""
    journals = _move_journals_aside(db_path)
    try:
        os.replace(temporary, db_path)
    except OSError:
        _restore_journals(journals)
        raise
    _discard_journals(journals)


def _verify_installed_copy(temporary: Path, expected_sha256: str) -> None:
    """Re-check the copy's integrity and the digest validated before the swap."""
    verify_integrity(temporary)
    actual = sha256_file(temporary)
    if actual != expected_sha256:
        raise BackupHashMismatchError(
            f"copied backup {temporary} changed after validation: "
            f"expected {expected_sha256}, actual {actual}"
        )


def _has_database(db_path: Path) -> bool:
    try:
        return db_path.is_file() and db_path.stat().st_size > 0
    except OSError:
        return False


def _warn_untightened_permissions(db_path: Path) -> None:
    """Warn, but never fail, when the restored file's mode cannot be tightened."""
    try:
        db_path.chmod(0o600)
    except OSError as exc:
        typer.echo(
            f"warning: restored {db_path} but could not tighten its permissions: {exc}",
            err=True,
        )


def _install_candidate(
    candidate: Path,
    expected_sha256: str,
    settings: Settings,
    db_path: Path,
    *,
    now: datetime,
    json_output: bool,
) -> Path | None:
    """Snapshot, verify, and atomically install ``candidate``; return the snapshot."""
    had_database = _has_database(db_path)
    safety_snapshot: Path | None = None
    temporary: Path | None = None
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.data_dir.chmod(0o700)
        if had_database:
            # The safety snapshot must complete before the live file is touched.
            safety_snapshot = snapshot_before_restore(db_path, now=now)
        temporary = _copy_candidate(candidate, settings.data_dir)
        _verify_installed_copy(temporary, expected_sha256)
        _swap_database(db_path, temporary)
        temporary = None
    except BackupError as exc:
        _emit_failure(exc, json_output=json_output)
    except OSError as exc:
        _emit_os_failure(
            f"restore failed while preparing {settings.data_dir}: {exc}",
            json_output=json_output,
        )
    finally:
        if temporary is not None:
            discard(temporary)
    _warn_untightened_permissions(db_path)
    return safety_snapshot


def _confirm_restore(candidate: Path, db_path: Path, *, yes: bool, json_output: bool) -> None:
    if yes:
        return
    if json_output:
        emit_error(
            "VALIDATION_FAILED",
            "restore requires --yes when --json is used",
            json_output=json_output,
        )
        raise typer.Exit(code=1)
    typer.confirm(
        f"Restore {candidate} over {db_path}? The current database is kept "
        "as a pre-restore snapshot.",
        abort=True,
    )


def _emit_restore_success(
    *,
    verified: BackupVerification,
    candidate: Path,
    db_path: Path,
    safety_snapshot: Path | None,
    json_output: bool,
) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    {
                        "restored": str(db_path),
                        "source": str(candidate),
                        "safety_snapshot": str(safety_snapshot) if safety_snapshot else None,
                        "size_bytes": verified.size_bytes,
                        "sha256": verified.sha256,
                    },
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
        return
    typer.echo(f"restored {db_path}")
    typer.echo(f"source   {candidate}")
    if safety_snapshot is not None:
        typer.echo(f"snapshot {safety_snapshot}")
    else:
        typer.echo("snapshot none (no existing database)")


def restore_command(
    source: Annotated[
        Path,
        typer.Argument(help="Backup file to restore (a .db produced by `oft backup`)."),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the restore without prompting."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the shared JSON envelope."),
    ] = False,
) -> None:
    """Restore the local database from a verified backup file."""
    settings = settings_or_exit(json_output)
    candidate = source.expanduser()
    try:
        verified = validate_candidate(candidate)
    except BackupError as exc:
        _emit_failure(exc, json_output=json_output)

    if _daemon_running(settings):
        emit_error("DAEMON_RUNNING", _DAEMON_RUNNING_MESSAGE, json_output=json_output)
        raise typer.Exit(code=1)

    db_path = database_path(settings)
    _confirm_restore(candidate, db_path, yes=yes, json_output=json_output)
    safety_snapshot = _install_candidate(
        candidate,
        verified.sha256,
        settings,
        db_path,
        now=datetime.now(UTC),
        json_output=json_output,
    )
    _emit_restore_success(
        verified=verified,
        candidate=candidate,
        db_path=db_path,
        safety_snapshot=safety_snapshot,
        json_output=json_output,
    )
