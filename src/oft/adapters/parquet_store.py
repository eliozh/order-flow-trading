"""Parquet/DuckDB placeholders for the analytical store."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from oft.config import Settings

if TYPE_CHECKING:
    import duckdb

_LAYOUT = ("market", "metrics", "footprint", "liquidations")


def ensure_layout(settings: Settings) -> tuple[Path, ...]:
    """Create the immutable-part directories under the data directory."""
    roots = tuple(settings.data_dir / name for name in _LAYOUT)
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


def open_duckdb_catalog(settings: Settings) -> duckdb.DuckDBPyConnection:
    """Open the local DuckDB catalog; views are added by later slices."""
    import duckdb

    catalog_dir = settings.data_dir / "duckdb"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(catalog_dir / "oft.duckdb"))
