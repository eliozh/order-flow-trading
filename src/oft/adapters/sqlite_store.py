"""SQLite placeholders for the transactional store.

No application schema exists yet; slice 01 only needs an initialization hook.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from oft.config import Settings


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "oft.db"


def initialize_database(settings: Settings) -> None:
    """Create the data directory and apply the documented connection pragmas.

    Tables and migrations arrive with the storage slice.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path(settings))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
    finally:
        connection.close()
