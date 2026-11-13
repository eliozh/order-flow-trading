"""Shared test support (not a test module, no fixtures).

The functions here are imported as ``position_support`` because the ``tests``
directory is on ``sys.path`` while pytest loads the root ``conftest``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def set_position_mark(
    target: Any,
    position_id: str,
    mark: str | None,
    as_of: str | None = None,
) -> None:
    """Rewrite a position row's stored mark/``updated_at`` directly in SQLite.

    ``target`` is a container (``.settings``) or a ``Settings`` value. This is
    only for tests that deliberately control the fallback (stored) mark; tests
    of the recorded-bar path seed a published bar part instead.
    """
    settings = getattr(target, "settings", target)
    database = settings.data_dir / "oft.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["mark_price"] = mark
        if as_of is not None:
            payload["updated_at"] = as_of
        connection.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(payload, sort_keys=True), position_id),
        )
        connection.commit()


__all__ = ["set_position_mark"]
