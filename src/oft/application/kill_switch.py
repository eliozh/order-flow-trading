"""Kill switch (DB + file sentinel) and the append-only audit chain.

Activation is immediate, writes ``$OFT_DATA/run/KILL_SWITCH`` (0600) and the
``system_state.kill_switch`` row, and never prompts. Deactivation requires the
typed ``RESUME`` token (ADR-0006).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from oft.config import Settings
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import AuditEventDraft, AuditEventRecord, StorePort

KILL_SWITCH_KEY = "kill_switch"
RESUME_TOKEN = "RESUME"


class KillSwitchActive(Exception):
    """New orders and retries are blocked while the kill switch is active."""

    code = "KILL_SWITCH_ACTIVE"

    def __init__(self, message: str = "The kill switch blocks new orders.") -> None:
        super().__init__(message)
        self.message = message


class InvalidResumeToken(Exception):
    code = "VALIDATION_FAILED"

    def __init__(self, message: str = "deactivation requires the typed RESUME token") -> None:
        super().__init__(message)
        self.message = message


def record_audit(
    store: StorePort,
    *,
    event_type: str,
    actor: dict[str, Any],
    subject: dict[str, Any],
    payload: dict[str, Any],
    at: str,
) -> AuditEventRecord:
    """Append one hash-chained audit event (previous hash -> event hash).

    The store computes the seq/previous hash and inserts inside one
    ``BEGIN IMMEDIATE`` transaction so concurrent writers cannot collide.
    """
    return store.append_audit_event(
        AuditEventDraft(
            event_type=event_type,
            actor=actor,
            subject=subject,
            payload=payload,
            created_at=at,
        )
    )


class KillSwitch:
    def __init__(self, *, settings: Settings, store: StorePort, clock: ClockPort) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._path = settings.data_dir / "run" / "KILL_SWITCH"

    @property
    def sentinel_path(self) -> Path:
        return self._path

    def is_active(self) -> bool:
        if self._path.exists():
            return True
        return self._store.get_system_state(KILL_SWITCH_KEY) == "1"

    def status(self) -> dict[str, Any]:
        return {
            "active": self.is_active(),
            "sentinel_path": self._path.name,
            "checked_at": utc_timestamp(self._clock.now()),
        }

    def activate(self, *, actor_id: str = "cli") -> dict[str, Any]:
        now = utc_timestamp(self._clock.now())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("1\n", encoding="utf-8")
        os.chmod(self._path, 0o600)
        self._store.set_system_state(KILL_SWITCH_KEY, "1", updated_at=now)
        record_audit(
            self._store,
            event_type="KILL_SWITCH_ON",
            actor={"kind": "USER", "id": actor_id},
            subject={"kind": "SYSTEM", "id": KILL_SWITCH_KEY},
            payload={"active": True},
            at=now,
        )
        return self.status()

    def deactivate(self, *, token: str, actor_id: str = "cli") -> dict[str, Any]:
        if token.strip() != RESUME_TOKEN:
            raise InvalidResumeToken()
        now = utc_timestamp(self._clock.now())
        if self._path.exists():
            self._path.unlink()
        self._store.set_system_state(KILL_SWITCH_KEY, "0", updated_at=now)
        record_audit(
            self._store,
            event_type="KILL_SWITCH_OFF",
            actor={"kind": "USER", "id": actor_id},
            subject={"kind": "SYSTEM", "id": KILL_SWITCH_KEY},
            payload={"active": False, "confirmation_token_used": True},
            at=now,
        )
        return self.status()

    def require_clear(self) -> None:
        if self.is_active():
            raise KillSwitchActive()
