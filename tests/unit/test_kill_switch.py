"""Kill switch: DB + file sentinel, RESUME confirmation, restart persistence."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.sqlite_store import SqliteStore
from oft.application.kill_switch import (
    InvalidResumeToken,
    KillSwitch,
    KillSwitchActive,
    record_audit,
)
from oft.clock import FixedClock
from oft.config import Settings

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def _client(tmp_path: Path) -> tuple[Settings, SqliteStore, KillSwitch]:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "data")
    store = SqliteStore(settings)
    store.initialize()
    return settings, store, KillSwitch(settings=settings, store=store, clock=FixedClock(NOW))


def test_activation_writes_the_db_and_a_0600_sentinel(tmp_path: Path) -> None:
    _settings, store, kill = _client(tmp_path)

    status = kill.activate()

    assert status["active"] is True
    assert kill.sentinel_path.exists()
    assert stat.S_IMODE(kill.sentinel_path.stat().st_mode) == 0o600
    assert store.get_system_state("kill_switch") == "1"
    with pytest.raises(KillSwitchActive):
        kill.require_clear()


def test_sentinel_survives_a_restart(tmp_path: Path) -> None:
    settings, store, kill = _client(tmp_path)
    kill.activate()

    restarted = KillSwitch(settings=settings, store=store, clock=FixedClock(NOW))

    assert restarted.is_active() is True


def test_db_state_alone_keeps_the_switch_active(tmp_path: Path) -> None:
    _settings, _store, kill = _client(tmp_path)
    kill.activate()
    kill.sentinel_path.unlink()

    assert kill.is_active() is True


def test_deactivation_requires_the_exact_resume_token(tmp_path: Path) -> None:
    _settings, _store, kill = _client(tmp_path)
    kill.activate()

    with pytest.raises(InvalidResumeToken):
        kill.deactivate(token="resume")
    with pytest.raises(InvalidResumeToken):
        kill.deactivate(token="")

    status = kill.deactivate(token="RESUME")

    assert status["active"] is False
    assert not kill.sentinel_path.exists()
    kill.require_clear()


def test_deactivation_is_idempotent_when_already_off(tmp_path: Path) -> None:
    _settings, store, kill = _client(tmp_path)

    status = kill.deactivate(token="RESUME")

    assert status["active"] is False
    assert store.get_system_state("kill_switch") == "0"


def test_audit_chain_links_each_event(tmp_path: Path) -> None:
    _settings, store, _kill = _client(tmp_path)

    first = record_audit(
        store,
        event_type="KILL_SWITCH_ON",
        actor={"kind": "USER", "id": "cli"},
        subject={"kind": "SYSTEM", "id": "kill_switch"},
        payload={"active": True},
        at="2026-11-06T12:00:00.000Z",
    )
    second = record_audit(
        store,
        event_type="KILL_SWITCH_OFF",
        actor={"kind": "USER", "id": "cli"},
        subject={"kind": "SYSTEM", "id": "kill_switch"},
        payload={"active": False},
        at="2026-11-06T12:01:00.000Z",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert second.previous_hash == first.event_hash
    assert store.last_audit_event() is not None
    assert store.last_audit_event().event_hash == second.event_hash  # type: ignore[union-attr]
