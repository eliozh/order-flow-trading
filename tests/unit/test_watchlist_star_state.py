"""Watchlist star state: idempotency, stable ``starred_at``, and one audit event."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.symbol_registry import SymbolRegistry
from oft.bootstrap import build_container
from oft.clock import FixedClock
from oft.config import Settings
from oft.domain.audit import AUDIT_HEAD_KEY
from oft.domain.time import utc_timestamp
from oft.ports.store import StarState, StoreUnavailableError

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NOW_TEXT = utc_timestamp(NOW)


class AdvancingClock:
    """A controllable clock so a no-op can prove it kept the first timestamp."""

    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant = self.instant + delta


def _registry(
    tmp_path: Path, clock: Any | None = None
) -> tuple[SymbolRegistry, SqliteStore, Settings]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    container = build_container(
        settings, initialize=True, clock=clock if clock is not None else FixedClock(NOW)
    )
    return container.symbol_registry, container.store, settings


def _audit_body(record: Any) -> dict[str, Any]:
    return json.loads(record.payload_json)


def test_star_writes_one_audit_event_with_before_and_after(tmp_path: Path) -> None:
    registry, store, _ = _registry(tmp_path)

    result = registry.set_star_with_audit("btcusdt", True, actor="web")

    assert result is not None
    assert result.changed is True
    assert result.symbol == "BTCUSDT"
    assert result.before == StarState(starred=False, starred_at=None)
    assert result.after == StarState(starred=True, starred_at=NOW_TEXT)

    audits = store.list_audit_events()
    assert len(audits) == 1
    assert audits[0].event_type == "WATCHLIST_STAR_TOGGLED"
    assert audits[0].created_at == NOW_TEXT
    body = _audit_body(audits[0])
    assert body["actor"] == {"kind": "USER", "id": "web"}
    assert body["subject"] == {"kind": "SYMBOL", "id": "BTCUSDT"}
    assert body["payload"] == {
        "symbol": "BTCUSDT",
        "before": {"starred": False, "starred_at": None},
        "after": {"starred": True, "starred_at": NOW_TEXT},
    }


def test_starring_twice_keeps_the_original_starred_at_and_one_audit(tmp_path: Path) -> None:
    clock = AdvancingClock(NOW)
    registry, store, _ = _registry(tmp_path, clock)

    first = registry.set_star_with_audit("BTCUSDT", True, actor="web")
    # Move the clock between the two calls: a rewrite of ``starred_at`` with the
    # request timestamp would now be observable.
    clock.advance(timedelta(hours=3))
    second = registry.set_star_with_audit("BTCUSDT", True, actor="cli")

    assert first is not None and second is not None
    assert first.changed is True
    assert second.changed is False
    assert second.before == second.after == StarState(starred=True, starred_at=NOW_TEXT)
    assert registry.get_star_state("BTCUSDT") == StarState(True, NOW_TEXT)
    assert registry.star_states()["BTCUSDT"] == StarState(True, NOW_TEXT)
    assert len(store.list_audit_events()) == 1


def test_audit_insert_failure_rolls_back_the_star_and_keeps_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, store, _ = _registry(tmp_path)
    registry.set_star_with_audit("ETHUSDT", True, actor="web")
    audits_before = store.list_audit_events()
    head_before = store.get_system_state(AUDIT_HEAD_KEY)

    def fail_audit_insert(connection: sqlite3.Connection, draft: Any) -> None:
        raise sqlite3.Error("audit write failed")

    monkeypatch.setattr(store, "_insert_audit_event", fail_audit_insert)

    with pytest.raises(StoreUnavailableError):
        registry.set_star_with_audit("BTCUSDT", True, actor="web")

    # The star update and the audit-chain head rolled back together.
    assert registry.get_star_state("BTCUSDT") == StarState(False, None)
    assert store.list_audit_events() == audits_before
    assert store.get_system_state(AUDIT_HEAD_KEY) == head_before

    # The failed transaction released its write lock: a follow-up write works.
    monkeypatch.undo()
    retried = registry.set_star_with_audit("BTCUSDT", True, actor="web")
    assert retried is not None and retried.changed is True
    assert len(store.list_audit_events()) == 2


def test_unstarring_twice_is_a_noop_and_clears_starred_at_once(tmp_path: Path) -> None:
    registry, store, _ = _registry(tmp_path)

    registry.set_star_with_audit("BTCUSDT", True, actor="web")
    first = registry.set_star_with_audit("BTCUSDT", False, actor="web")
    second = registry.set_star_with_audit("BTCUSDT", False, actor="web")

    assert first is not None and second is not None
    assert first.changed is True
    assert first.after == StarState(starred=False, starred_at=None)
    assert second.changed is False
    assert second.after == StarState(starred=False, starred_at=None)
    assert registry.get_star_state("BTCUSDT") == StarState(False, None)
    assert len(store.list_audit_events()) == 2


def test_unknown_symbol_returns_none_and_writes_no_audit(tmp_path: Path) -> None:
    registry, store, _ = _registry(tmp_path)

    assert registry.set_star_with_audit("NOPEUSDT", True, actor="web") is None
    assert registry.get_star_state("NOPEUSDT") is None
    assert store.list_audit_events() == ()
    assert registry.list_starred() == ()


def test_star_survives_a_registry_refresh(tmp_path: Path) -> None:
    registry, _, settings = _registry(tmp_path)
    registry.set_star_with_audit("BTCUSDT", True, actor="web")

    registry.refresh_from_fixture(settings.exchange_info_fixture)

    state = registry.get_star_state("BTCUSDT")
    assert state is not None
    assert state.starred is True
    assert state.starred_at == NOW_TEXT
    assert [item.symbol for item in registry.list_starred()] == ["BTCUSDT"]
