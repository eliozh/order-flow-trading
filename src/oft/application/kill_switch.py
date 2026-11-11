"""Kill switch (DB + file sentinel) and the append-only audit chain.

Activation is immediate, writes ``$OFT_DATA/run/KILL_SWITCH`` (0600) and the
``system_state.kill_switch`` row, and never prompts. Deactivation requires the
typed ``RESUME`` token (ADR-0006).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oft.config import Settings
from oft.domain.audit import AUDIT_HEAD_KEY, GENESIS_HASH, build_audit_entry
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import AuditEventDraft, AuditEventRecord, StorePort

KILL_SWITCH_KEY = "kill_switch"
RESUME_TOKEN = "RESUME"


@dataclass(frozen=True)
class AuditChainVerification:
    """The result of re-deriving every hash in the append-only chain (D7)."""

    ok: bool
    checked: int
    first_broken_seq: int | None
    message: str


def verify_audit_chain(store: StorePort) -> AuditChainVerification:
    """Verify seq continuity, previous-hash links, and recomputed event hashes."""
    initialized = getattr(store, "audit_store_initialized", None)
    if callable(initialized) and not initialized():
        # A missing database reads as an empty event tuple, which is not the
        # same thing as a valid empty chain (S5). Report the truth.
        return AuditChainVerification(
            ok=False,
            checked=0,
            first_broken_seq=None,
            message="AUDIT_CHAIN_UNAVAILABLE: the audit store is not initialized",
        )
    try:
        events = store.list_audit_events_ascending()
    except Exception as exc:  # pragma: no cover - defensive boundary
        return AuditChainVerification(
            ok=False,
            checked=0,
            first_broken_seq=None,
            message=f"AUDIT_CHAIN_UNAVAILABLE: the audit store could not be read ({exc})",
        )
    expected_previous = GENESIS_HASH
    checked = 0
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=event.seq,
                message=(f"AUDIT_CHAIN_BROKEN at seq {event.seq}: expected seq {expected_seq}"),
            )
        if event.previous_hash != expected_previous:
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=event.seq,
                message=f"AUDIT_CHAIN_BROKEN at seq {event.seq}: previous hash mismatch",
            )
        try:
            body = json.loads(event.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=event.seq,
                message=f"AUDIT_CHAIN_BROKEN at seq {event.seq}: payload is unreadable",
            )
        if not isinstance(body, dict):
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=event.seq,
                message=f"AUDIT_CHAIN_BROKEN at seq {event.seq}: payload is not an object",
            )
        recomputed = build_audit_entry(
            previous_seq=expected_seq - 1,
            previous_hash=event.previous_hash,
            event_type=event.event_type,
            actor=body.get("actor") or {},
            subject=body.get("subject") or {},
            payload=body.get("payload") or {},
            created_at=event.created_at,
        )
        if recomputed.event_hash != event.event_hash:
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=event.seq,
                message=f"AUDIT_CHAIN_BROKEN at seq {event.seq}: event hash does not recompute",
            )
        expected_previous = event.event_hash
        checked += 1
    # Compare the stored high-water mark: deleting the newest rows leaves the
    # upward seq scan intact but cannot hide the missing tail (S4).
    raw_head = store.get_system_state(AUDIT_HEAD_KEY)
    if raw_head is None:
        if events:
            # A pre-v20 chain upgraded in place has events but no anchor until
            # the first v20-era write; the tail cannot be verified, so this is
            # never reported as OK (S8).
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=None,
                message=(
                    "AUDIT_CHAIN_UNAVAILABLE: the stored audit head is missing "
                    "for a non-empty chain"
                ),
            )
    else:
        try:
            head = json.loads(raw_head)
            head_seq = int(head["seq"])
            head_hash = str(head["event_hash"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=None,
                message="AUDIT_CHAIN_BROKEN: the stored audit head is unreadable",
            )
        last = events[-1] if events else None
        actual_seq = last.seq if last is not None else 0
        actual_hash = last.event_hash if last is not None else GENESIS_HASH
        if head_seq != actual_seq or head_hash != actual_hash:
            return AuditChainVerification(
                ok=False,
                checked=checked,
                first_broken_seq=head_seq,
                message=(
                    f"AUDIT_CHAIN_BROKEN at seq {head_seq}: the stored audit head "
                    "does not match the chain (tail truncation)"
                ),
            )
    return AuditChainVerification(
        ok=True,
        checked=checked,
        first_broken_seq=None,
        message=f"AUDIT_CHAIN_OK ({checked} events)",
    )


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
