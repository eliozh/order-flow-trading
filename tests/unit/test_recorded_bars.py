"""The shared recorded-bar helper: series discovery, caching, and failure flags."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oft.application import recorded_bars
from oft.config import Settings
from oft.ports.store import StoreUnavailableError

SYMBOL = "BTCUSDT"
START = "2026-11-14T10:00:00.000Z"
END = "2026-11-14T12:00:00.000Z"


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", port=8765, data_dir=tmp_path / "data")


def _entry(
    period: str,
    *,
    symbol: str = SYMBOL,
    data_type: str = "bars",
    status: str = "PUBLISHED",
    metric_version: str | None = "ofi.v1.deadbeef",
) -> SimpleNamespace:
    version = f"/metric_version={metric_version}" if metric_version else ""
    return SimpleNamespace(
        data_type=data_type,
        symbol=symbol,
        status=status,
        path=f"data/bars/symbol={symbol}/period={period}{version}/date=2026-11-14/part.parquet",
        metric_version=metric_version,
    )


class _Store:
    def __init__(self, entries: tuple[Any, ...] = (), *, fail: bool = False) -> None:
        self._entries = entries
        self._fail = fail
        self.calls: list[str | None] = []

    def list_manifest(self, *, symbol: str | None = None) -> tuple[Any, ...]:
        self.calls.append(symbol)
        if self._fail:
            raise StoreUnavailableError("manifest unavailable")
        return tuple(entry for entry in self._entries if symbol is None or entry.symbol == symbol)


def test_bar_series_is_symbol_scoped_and_skips_unusable_entries() -> None:
    store = _Store(
        (
            _entry("5m"),
            _entry("5m"),  # duplicate series collapses
            _entry("1h", symbol="ETHUSDT"),
            _entry("1h", data_type="metrics"),
            _entry("1h", status="ORPHANED"),
            _entry("1h", metric_version=None),
        )
    )

    series, failed = recorded_bars.bar_series(store, SYMBOL)  # type: ignore[arg-type]

    assert series == {("5m", "ofi.v1.deadbeef")}
    assert failed is False
    assert store.calls == [SYMBOL]


def test_bar_series_reports_a_manifest_failure() -> None:
    series, failed = recorded_bars.bar_series(  # type: ignore[arg-type]
        _Store(fail=True), SYMBOL
    )

    assert series == set()
    assert failed is True


def test_read_series_caches_one_read_per_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    rows = ({"bar_end": START, "close": "70000"},)

    def fake_read_bars(_settings: Settings, **kwargs: Any) -> tuple[dict[str, object], ...]:
        calls.append(str(kwargs["period"]))
        return rows

    monkeypatch.setattr(recorded_bars, "read_bars", fake_read_bars)
    cache: recorded_bars.SeriesCache = {}

    first = recorded_bars.read_series(
        _settings(tmp_path),
        _Store(),
        SYMBOL,
        "5m",
        "ofi.v1.deadbeef",
        cache,  # type: ignore[arg-type]
    )
    second = recorded_bars.read_series(
        _settings(tmp_path),
        _Store(),
        SYMBOL,
        "5m",
        "ofi.v1.deadbeef",
        cache,  # type: ignore[arg-type]
    )

    assert first == second
    assert first.rows == rows
    assert first.failed is False
    assert calls == ["5m"]


def test_read_series_reports_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> tuple[dict[str, object], ...]:
        raise OSError("part unreadable")

    monkeypatch.setattr(recorded_bars, "read_bars", explode)
    cache: recorded_bars.SeriesCache = {}

    result = recorded_bars.read_series(
        _settings(tmp_path),
        _Store(),
        SYMBOL,
        "5m",
        "ofi.v1.deadbeef",
        cache,  # type: ignore[arg-type]
    )

    assert result.rows == ()
    assert result.failed is True
    # The failure is cached too, so one operation does not retry a broken part.
    assert cache[("5m", "ofi.v1.deadbeef")] is result


def test_window_bars_prefers_the_series_with_the_newest_in_window_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_period = {
        "5m": ({"bar_end": "2026-11-14T11:05:00.000Z", "close": "1"},),
        "1h": ({"bar_end": "2026-11-14T11:30:00.000Z", "close": "2"},),
    }
    monkeypatch.setattr(
        recorded_bars,
        "read_bars",
        lambda _settings, **kwargs: by_period[str(kwargs["period"])],
    )
    store = _Store((_entry("5m"), _entry("1h")))

    window = recorded_bars.window_bars(
        _settings(tmp_path),
        store,
        SYMBOL,
        START,
        END,  # type: ignore[arg-type]
    )

    assert window.rows == by_period["1h"]
    assert window.unavailable is False
    assert bool(window) is True


def test_window_bars_marks_unavailable_when_a_candidate_series_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_read_bars(_settings: Settings, **kwargs: Any) -> tuple[dict[str, object], ...]:
        if kwargs["period"] == "1h":
            raise StoreUnavailableError("newest series unreadable")
        return ({"bar_end": "2026-11-14T10:30:00.000Z", "close": "1"},)

    monkeypatch.setattr(recorded_bars, "read_bars", fake_read_bars)
    store = _Store((_entry("5m"), _entry("1h")))

    window = recorded_bars.window_bars(
        _settings(tmp_path),
        store,
        SYMBOL,
        START,
        END,  # type: ignore[arg-type]
    )

    # The surviving older series must not be presented as a measured result.
    assert window.unavailable is True
    assert window.rows


def test_window_bars_marks_unavailable_when_the_manifest_fails(tmp_path: Path) -> None:
    window = recorded_bars.window_bars(
        _settings(tmp_path),
        _Store(fail=True),
        SYMBOL,
        START,
        END,  # type: ignore[arg-type]
    )

    assert window.rows == ()
    assert window.unavailable is True
    assert bool(window) is False


def test_window_bars_rejects_unparseable_bounds(tmp_path: Path) -> None:
    window = recorded_bars.window_bars(
        _settings(tmp_path),
        _Store(),
        SYMBOL,
        None,
        END,  # type: ignore[arg-type]
    )

    assert window.rows == ()
    assert window.unavailable is False


def test_timestamp_and_period_parsing() -> None:
    assert recorded_bars.parse_timestamp("2026-11-14T12:00:00Z").isoformat().endswith("+00:00")
    assert recorded_bars.parse_timestamp("not-a-time") is None
    assert recorded_bars.parse_timestamp(None) is None
    assert (
        recorded_bars.period_from_path(
            "data/bars/symbol=BTCUSDT/period=5m/metric_version=ofi.v1/date=x/p.parquet"
        )
        == "5m"
    )
    assert recorded_bars.period_from_path("no-period-here") is None
