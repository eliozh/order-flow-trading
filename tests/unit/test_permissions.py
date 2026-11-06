"""Filesystem permissions: the data dir is 0700 and the DB is 0600."""

from __future__ import annotations

import stat
from pathlib import Path

from oft.bootstrap import build_container
from oft.config import Settings


def test_initialize_tightens_a_pre_created_data_dir_and_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    settings = Settings(env="test", port=8765, data_dir=data_dir)

    build_container(settings, initialize=True)

    db_path = data_dir / "oft.db"
    assert db_path.is_file()
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
