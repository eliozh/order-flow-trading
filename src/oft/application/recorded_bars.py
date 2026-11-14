"""Shared recorded-bar selection for the position and journal read models.

Both services need the same four steps: enumerate the published bar series for
one symbol from the manifest, read one series (cached within an operation), pick
the bars whose ``bar_end`` falls inside a holding window, and prefer the series
whose newest in-window bar is the most recent. This module owns that logic once
so a future no-lookahead or series-selection fix cannot drift between them.

Failures are reported, never hidden: a manifest read that fails, or any
candidate series that cannot be read, marks the whole window ``unavailable`` so
the caller can report ``N/A`` with a distinct reason instead of deriving a
measured-looking result from an older surviving series.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from oft.adapters.parquet_store import read_bars
from oft.config import Settings
from oft.ports.store import StorePort, StoreUnavailableError

_LOGGER = logging.getLogger(__name__)

BARS_STATUSES = frozenset({"PUBLISHED", "COMPLETE"})
_PERIOD_TOKEN = re.compile(r"(?:^|/)period=([^/]+)(?:/|$)")

SeriesKey = tuple[str, str]
BarRows = tuple[dict[str, object], ...]
SeriesCache = dict[SeriesKey, "SeriesRead"]


@dataclass(frozen=True)
class SeriesRead:
    """One logical series' rows plus whether the read failed."""

    rows: BarRows
    failed: bool = False


@dataclass(frozen=True)
class WindowBars:
    """The selected in-window rows plus whether any read failed."""

    rows: BarRows
    unavailable: bool = False

    def __bool__(self) -> bool:
        return bool(self.rows)


def parse_timestamp(value: object) -> datetime | None:
    """Parse a recorded timestamp to an aware UTC datetime, or ``None``."""
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def period_from_path(path: str) -> str | None:
    """The ``period=`` token from a manifest path, or ``None``."""
    match = _PERIOD_TOKEN.search(path)
    return match.group(1) if match is not None else None


def bar_series(store: StorePort, symbol: str) -> tuple[set[SeriesKey], bool]:
    """The published ``(period, metric_version)`` series for one symbol.

    Returns ``(series, failed)``; ``failed`` is true when the symbol-scoped
    manifest read itself failed. The manifest query is symbol-filtered so one
    compare/tick does not scan every symbol's manifest.
    """
    try:
        entries = store.list_manifest(symbol=symbol)
    except StoreUnavailableError as exc:
        _LOGGER.warning("cannot read the manifest for %s: %s", symbol, exc)
        return set(), True
    series: set[SeriesKey] = set()
    for entry in entries:
        if entry.data_type != "bars" or entry.symbol != symbol or entry.status not in BARS_STATUSES:
            continue
        period = period_from_path(entry.path)
        if period is None or entry.metric_version is None:
            continue
        series.add((period, entry.metric_version))
    return series, False


def read_series(
    settings: Settings,
    store: StorePort,
    symbol: str,
    period: str,
    metric_version: str,
    cache: SeriesCache | None = None,
) -> SeriesRead:
    """Read one logical bars series, reusing it within one operation."""
    key = (period, metric_version)
    if cache is not None and key in cache:
        return cache[key]
    try:
        rows = read_bars(
            settings,
            symbol=symbol,
            period=period,
            metric_version=metric_version,
            store=store,
        )
        result = SeriesRead(rows=rows)
    except (OSError, ValueError, StoreUnavailableError) as exc:
        _LOGGER.warning("cannot read bars for %s: %s", symbol, exc)
        result = SeriesRead(rows=(), failed=True)
    if cache is not None:
        cache[key] = result
    return result


def window_bars(
    settings: Settings,
    store: StorePort,
    symbol: str,
    start: object,
    end: object,
    *,
    series: set[SeriesKey] | None = None,
    cache: SeriesCache | None = None,
) -> WindowBars:
    """The recorded bars whose ``bar_end`` falls inside the holding window."""
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    if not symbol or start_dt is None or end_dt is None:
        return WindowBars(rows=())
    if series is None:
        resolved_series, manifest_failed = bar_series(store, symbol)
        unavailable = manifest_failed
    else:
        resolved_series = series
        unavailable = False
    best: BarRows = ()
    best_end: datetime | None = None
    for period, metric_version in sorted(resolved_series):
        read = read_series(settings, store, symbol, period, metric_version, cache)
        unavailable = unavailable or read.failed
        usable: list[dict[str, object]] = []
        newest: datetime | None = None
        for row in read.rows:
            bar_end = parse_timestamp(row.get("bar_end"))
            if bar_end is None or not (start_dt <= bar_end <= end_dt):
                continue
            usable.append(row)
            if newest is None or bar_end > newest:
                newest = bar_end
        if newest is not None and (best_end is None or newest > best_end):
            best_end = newest
            best = tuple(usable)
    return WindowBars(rows=best, unavailable=unavailable)


__all__ = [
    "BARS_STATUSES",
    "BarRows",
    "SeriesCache",
    "SeriesKey",
    "SeriesRead",
    "WindowBars",
    "bar_series",
    "parse_timestamp",
    "period_from_path",
    "read_series",
    "window_bars",
]
