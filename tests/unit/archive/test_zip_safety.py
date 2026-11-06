"""Hostile archive handling: traversal, absolute paths, symlinks, bombs."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from oft.adapters.archive import ExtractionLimits, extract_zip_safely
from oft.ports.archive import UnsafeArchiveError

ARCHIVE = Path(__file__).resolve().parents[2] / "fixtures" / "archive"
VALID_ZIP = ARCHIVE / "btcusdt_agg-trades_2026-10-01.zip"


def _zip(path: Path, entries: list[tuple[str, bytes]], *, mode: int | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode if mode is not None else 0o600) << 16
            archive.writestr(info, payload)
    return path


def _zip_info(path: Path, name: str, payload: bytes, *, flags: int = 0) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        info.flag_bits |= flags
        archive.writestr(info, payload)
    if flags & 0x1:
        _set_encrypted_flag(path)
    return path


def _set_encrypted_flag(path: Path) -> None:
    """Set the ZIP encryption bit in the local and central headers.

    ``zipfile`` clears custom flag bits on write, so a real encrypted-looking
    entry must be produced by patching the two header records.
    """
    data = bytearray(path.read_bytes())
    local = data.find(b"PK\x03\x04")
    central = data.find(b"PK\x01\x02")
    data[local + 6] |= 0x1
    data[central + 8] |= 0x1
    path.write_bytes(bytes(data))


def test_valid_archive_extracts_into_a_new_empty_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = extract_zip_safely(VALID_ZIP, workspace=workspace)

    assert result.directory.parent == workspace
    assert result.directory != workspace
    assert result.directory.is_dir()
    assert len(result.files) == 1
    assert result.files[0].suffix == ".csv"
    assert result.uncompressed_bytes > 0


def test_zip_slip_fixture_is_rejected_before_any_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeArchiveError) as excinfo:
        extract_zip_safely(ARCHIVE / "zip_slip.zip", workspace=workspace)

    assert "traversal" in str(excinfo.value).lower() or "path" in str(excinfo.value).lower()
    assert list(workspace.iterdir()) == []
    assert not (tmp_path / "zip_slip_escape.txt").exists()


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "C:\\Windows\\system32\\evil.txt", "..\\..\\evil.txt", "a/../../evil.txt"],
)
def test_absolute_drive_and_backslash_paths_are_rejected(tmp_path: Path, name: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = _zip(tmp_path / "hostile.zip", [(name, b"evil")])

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(archive_path, workspace=workspace)
    assert list(workspace.iterdir()) == []


def test_symlink_entries_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = _zip(
        tmp_path / "symlink.zip",
        [("link.csv", b"/etc/passwd")],
        mode=stat.S_IFLNK | 0o777,
    )

    with pytest.raises(UnsafeArchiveError) as excinfo:
        extract_zip_safely(archive_path, workspace=workspace)

    assert "symlink" in str(excinfo.value).lower()
    assert list(workspace.iterdir()) == []


def test_encrypted_entries_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = _zip_info(tmp_path / "encrypted.zip", "secret.csv", b"data", flags=0x1)

    with pytest.raises(UnsafeArchiveError) as excinfo:
        extract_zip_safely(archive_path, workspace=workspace)

    assert "encrypted" in str(excinfo.value).lower()


def test_directory_entries_are_rejected_as_non_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "directory.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("nested/", date_time=(2026, 10, 1, 0, 0, 0))
        archive.writestr(info, b"")

    with pytest.raises(UnsafeArchiveError) as excinfo:
        extract_zip_safely(path, workspace=workspace)

    assert "non-file" in str(excinfo.value).lower()


def test_entry_count_limit_is_enforced(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = _zip(tmp_path / "many.zip", [(f"{index}.csv", b"x") for index in range(3)])

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(
            archive_path, workspace=workspace, limits=ExtractionLimits(max_entries=2)
        )
    assert list(workspace.iterdir()) == []


def test_oversize_fixture_is_rejected_by_the_uncompressed_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeArchiveError) as excinfo:
        extract_zip_safely(
            ARCHIVE / "oversize.zip",
            workspace=workspace,
            limits=ExtractionLimits(max_uncompressed_bytes=64 * 1024),
        )

    assert "size" in str(excinfo.value).lower() or "expand" in str(excinfo.value).lower()
    assert list(workspace.iterdir()) == []


def test_expansion_ratio_limit_is_enforced(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(
            ARCHIVE / "oversize.zip",
            workspace=workspace,
            limits=ExtractionLimits(max_expansion_ratio=2.0),
        )


def test_extraction_never_executes_archive_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "executed.marker"
    payload = f"open({str(marker)!r}, 'w').write('pwned')\n".encode()
    archive_path = _zip(tmp_path / "code.zip", [("payload.py", payload)])

    result = extract_zip_safely(archive_path, workspace=workspace)

    assert result.files[0].read_bytes() == payload
    assert not marker.exists()


def test_rejected_destination_directory_is_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeArchiveError):
        extract_zip_safely(ARCHIVE / "zip_slip.zip", workspace=workspace)

    # Nothing remains: the temp extraction directory is cleaned up on failure.
    assert list(workspace.iterdir()) == []
