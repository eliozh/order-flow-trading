"""Read model for the daemon-independent ``ws_state.json`` surface.

This module is deliberately I/O-light and dependency-free (settings + JSON) so
both the ingestion supervisor and the scan freshness gate can read the same
file without importing each other.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from oft.config import Settings

RUN_DIRECTORY = "run"
WS_STATE_FILENAME = "ws_state.json"


class WsRuntimeState:
    """Read model of the last written WS state file."""

    def __init__(
        self,
        *,
        connection_state: str,
        streams: Mapping[str, Mapping[str, object]],
        freshness: Mapping[str, Mapping[str, object]],
        updated_at: str,
        stale: bool,
        stats: Mapping[str, int] | None = None,
    ) -> None:
        self.connection_state = connection_state
        self.streams = {key: dict(value) for key, value in streams.items()}
        self.freshness = {key: dict(value) for key, value in freshness.items()}
        self.updated_at = updated_at
        self.stale = stale
        self.stats = dict(stats or {})

    def to_dict(self) -> dict[str, object]:
        return {
            "connection_state": self.connection_state,
            "streams": self.streams,
            "freshness": self.freshness,
            "updated_at": self.updated_at,
            "stale": self.stale,
            "stats": self.stats,
        }


def run_directory(settings: Settings) -> Path:
    """Directory holding runtime state files (schedule, WS state, checkpoints)."""
    return settings.data_dir / RUN_DIRECTORY


def read_ws_state(settings: Settings) -> WsRuntimeState | None:
    """Read the last WS state file; ``None`` when it is absent or unreadable."""
    path = run_directory(settings) / WS_STATE_FILENAME
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    streams = document.get("streams")
    freshness = document.get("freshness")
    raw_stats = document.get("stats")
    stats = (
        {str(key): int(value) for key, value in raw_stats.items() if isinstance(value, int)}
        if isinstance(raw_stats, dict)
        else {}
    )
    return WsRuntimeState(
        connection_state=str(document.get("connection_state", "UNKNOWN")),
        streams=streams if isinstance(streams, dict) else {},
        freshness=freshness if isinstance(freshness, dict) else {},
        updated_at=str(document.get("updated_at", "")),
        stale=bool(document.get("stale", False)),
        stats=stats,
    )


__all__ = [
    "RUN_DIRECTORY",
    "WS_STATE_FILENAME",
    "WsRuntimeState",
    "read_ws_state",
    "run_directory",
]
