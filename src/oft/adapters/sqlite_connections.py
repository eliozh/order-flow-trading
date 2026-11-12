"""SQLite connection mixin: read-write and read-only store handles.

Owns the pragma set (busy timeout, foreign keys) every connection uses. The
concrete store supplies ``_settings`` and ``_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from oft.config import Settings

_BUSY_TIMEOUT_MS = 5000


class SqliteConnectionMixin:
    """Open read-write and read-only connections with consistent pragmas."""

    _settings: Settings
    _path: Path

    def _connect(self) -> sqlite3.Connection:
        settings = self._settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection | None:
        if not self._path.exists():
            return None
        connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return connection
