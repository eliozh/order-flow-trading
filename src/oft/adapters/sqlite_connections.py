"""SQLite connection mixin: read-write and read-only store handles.

Read-write handles apply the busy timeout and enable foreign keys; read-only
handles (``mode=ro``, used by diagnostics) apply the busy timeout only. The
concrete store supplies ``_settings`` and ``_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from oft.config import Settings

_BUSY_TIMEOUT_MS = 5000


def configure_connection(connection: sqlite3.Connection, *, foreign_keys: bool = True) -> None:
    """Apply the shared pragma set to a freshly opened connection.

    Foreign keys are enabled by default because every read-write handle uses
    them; read-only handles pass ``foreign_keys=False`` so their pragma
    sequence keeps ``PRAGMA foreign_keys`` untouched. Recursive triggers are
    always enabled so ``INSERT OR REPLACE`` fires the append-only delete
    triggers instead of silently rewriting an immutable row (D7).
    """
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA recursive_triggers=ON")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")


class SqliteConnectionMixin:
    """Open read-write and read-only connections with consistent pragmas."""

    _settings: Settings
    _path: Path

    def _connect(self) -> sqlite3.Connection:
        settings = self._settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        configure_connection(connection)
        return connection

    def _connect_read_only(self) -> sqlite3.Connection | None:
        if not self._path.exists():
            return None
        connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        configure_connection(connection, foreign_keys=False)
        return connection
