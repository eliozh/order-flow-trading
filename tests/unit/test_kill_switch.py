"""Kill switch: DB + file sentinel, RESUME confirmation, restart persistence."""

from __future__ import annotations

import json
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oft.adapters.fixture_market_data import FixtureMarketData
from oft.adapters.sqlite_store import SqliteStore
from oft.application.health import DaemonProbe, build_doctor
from oft.application.kill_switch import (
    InvalidResumeToken,
    KillSwitch,
    KillSwitchActive,
    record_audit,
    verify_audit_chain,
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


def test_activation_and_deactivation_audit_themselves(tmp_path: Path) -> None:
    _settings, store, kill = _client(tmp_path)

    kill.activate(actor_id="ops")
    kill.deactivate(token="RESUME", actor_id="ops")

    events = store.list_audit_events_ascending()
    assert [event.event_type for event in events] == ["KILL_SWITCH_ON", "KILL_SWITCH_OFF"]
    off_payload = json.loads(events[1].payload_json)["payload"]
    assert off_payload == {"active": False, "confirmation_token_used": True}


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


def _audit_pair(store: SqliteStore) -> None:
    record_audit(
        store,
        event_type="KILL_SWITCH_ON",
        actor={"kind": "USER", "id": "cli"},
        subject={"kind": "SYSTEM", "id": "kill_switch"},
        payload={"active": True},
        at="2026-11-06T12:00:00.000Z",
    )
    record_audit(
        store,
        event_type="KILL_SWITCH_OFF",
        actor={"kind": "USER", "id": "cli"},
        subject={"kind": "SYSTEM", "id": "kill_switch"},
        payload={"active": False},
        at="2026-11-06T12:01:00.000Z",
    )


def _tamper(settings: Settings, *statements: str) -> None:
    """Simulate an attacker with raw DB access: drop the guards, then corrupt."""
    db_path = settings.data_dir / "oft.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        connection.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
        for statement in statements:
            connection.execute(statement)


def test_audit_events_reject_update_and_delete(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    db_path = settings.data_dir / "oft.db"
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audit_events SET payload_json = '{}' WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_events WHERE seq = 1")

    # The rejected writes left the chain intact.
    assert verify_audit_chain(store).ok is True
    assert len(store.list_audit_events_ascending()) == 2


def test_verify_audit_chain_detects_a_truncated_tail(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    # The delete guard must be dropped to simulate a raw-DB rewrite; the
    # out-of-band high-water mark still catches the missing newest row.
    _tamper(settings, "DELETE FROM audit_events WHERE seq = 2")

    result = verify_audit_chain(store)

    assert result.ok is False
    assert "tail truncation" in result.message
    assert result.checked == 1


def test_verify_audit_chain_never_reports_ok_for_a_headless_upgraded_chain(
    tmp_path: Path,
) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    # A pre-v20 database upgraded in place has events but no high-water mark;
    # truncating the tail must not silently pass.
    with sqlite3.connect(settings.data_dir / "oft.db") as connection:
        connection.execute("DELETE FROM system_state WHERE key = 'audit_head'")
        connection.commit()
    _tamper(settings, "DELETE FROM audit_events WHERE seq = 2")

    result = verify_audit_chain(store)

    assert result.ok is False
    assert "AUDIT_CHAIN_UNAVAILABLE" in result.message
    assert "AUDIT_CHAIN_OK" not in result.message


def test_verify_audit_chain_detects_a_previous_hash_mismatch(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    _tamper(
        settings,
        "UPDATE audit_events SET previous_hash = '" + "f" * 64 + "' WHERE seq = 2",
    )

    result = verify_audit_chain(store)

    assert result.ok is False
    assert result.first_broken_seq == 2
    assert "previous hash mismatch" in result.message


def test_verify_audit_chain_reports_an_uninitialized_store(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "missing")
    store = SqliteStore(settings)

    result = verify_audit_chain(store)

    assert result.ok is False
    assert "AUDIT_CHAIN_UNAVAILABLE" in result.message
    assert "initialized" in result.message


def test_verify_audit_chain_accepts_a_intact_chain(tmp_path: Path) -> None:
    _settings, store, _kill = _client(tmp_path)
    _audit_pair(store)

    result = verify_audit_chain(store)

    assert result.ok is True
    assert result.checked == 2
    assert result.first_broken_seq is None
    assert "AUDIT_CHAIN_OK" in result.message


def test_verify_audit_chain_detects_a_tampered_payload(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    _tamper(
        settings,
        "UPDATE audit_events SET payload_json = "
        '\'{"actor": {}, "subject": {}, "payload": {"tampered": true}}\' WHERE seq = 1',
    )

    result = verify_audit_chain(store)

    assert result.ok is False
    assert result.first_broken_seq == 1
    assert "AUDIT_CHAIN_BROKEN" in result.message


def test_doctor_reports_an_uninitialized_audit_store_as_degraded(tmp_path: Path) -> None:
    settings = Settings(env="test", port=8765, data_dir=tmp_path / "missing")
    store = SqliteStore(settings)

    report = build_doctor(
        settings,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    check = {item.name: item for item in report.checks}["audit.chain"]

    assert check.status == "degraded"
    assert "AUDIT_CHAIN_UNAVAILABLE" in check.detail
    assert report.status == "DEGRADED"


def test_verify_audit_chain_detects_a_broken_sequence(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    _tamper(settings, "UPDATE audit_events SET seq = 7 WHERE seq = 2")

    result = verify_audit_chain(store)

    assert result.ok is False
    assert result.first_broken_seq == 7


def test_doctor_reports_the_audit_chain_and_no_secrets(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)

    report = build_doctor(
        settings,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    checks = {check.name: check for check in report.checks}

    assert checks["audit.chain"].status == "ok"
    assert "AUDIT_CHAIN_OK" in checks["audit.chain"].detail
    assert checks["config.secrets"].status == "ok"
    assert "no secrets" in checks["config.secrets"].detail


def test_doctor_flags_a_tampered_chain(tmp_path: Path) -> None:
    settings, store, _kill = _client(tmp_path)
    _audit_pair(store)
    _tamper(settings, "UPDATE audit_events SET event_hash = '" + "0" * 64 + "' WHERE seq = 1")

    report = build_doctor(
        settings,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    check = {item.name: item for item in report.checks}["audit.chain"]

    assert check.status == "degraded"
    assert "AUDIT_CHAIN_BROKEN" in check.detail
    assert report.status == "DEGRADED"


def test_doctor_never_echoes_a_configured_secret_value(tmp_path: Path) -> None:
    _settings, store, _kill = _client(tmp_path)
    secret = "sk-doctor-sentinel-do-not-leak"
    configured = Settings(
        env="test",
        port=8765,
        data_dir=tmp_path / "data",
        api_token=secret,
        openai_api_key=secret,
    )

    report = build_doctor(
        configured,
        store,
        FixtureMarketData(),
        FixedClock(NOW),
        daemon=DaemonProbe(state="UP", detail="test listener"),
    )
    checks = {check.name: check for check in report.checks}

    assert checks["config.secrets"].detail == "2 secret(s) configured (values not displayed)"
    for check in report.checks:
        assert secret not in check.detail
        assert secret not in check.name
