"""Append-only audit hash chain (ADR-0006).

The chain value is a pure function of the previous event's seq/hash and the
new event body, so both the application auditor and the transactional store
can compute it inside their own unit of work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

GENESIS_HASH = "0" * 64
# The ``system_state`` key holding the last (seq, event_hash) written. It lives
# outside the append-only table so tail truncation is detectable.
AUDIT_HEAD_KEY = "audit_head"


@dataclass(frozen=True)
class AuditChainEntry:
    event_id: str
    seq: int
    previous_hash: str
    event_hash: str
    payload_json: str


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_audit_entry(
    *,
    previous_seq: int | None,
    previous_hash: str | None,
    event_type: str,
    actor: dict[str, Any],
    subject: dict[str, Any],
    payload: dict[str, Any],
    created_at: str,
) -> AuditChainEntry:
    seq = 1 if previous_seq is None else previous_seq + 1
    resolved_previous_hash = GENESIS_HASH if previous_hash is None else previous_hash
    body = {
        "seq": seq,
        "event_type": event_type,
        "actor": actor,
        "subject": subject,
        "payload": payload,
        "created_at": created_at,
    }
    event_hash = hashlib.sha256(f"{resolved_previous_hash}:{_canonical(body)}".encode()).hexdigest()
    return AuditChainEntry(
        event_id=f"AUD-{seq:08d}-{event_hash[:8].upper()}",
        seq=seq,
        previous_hash=resolved_previous_hash,
        event_hash=event_hash,
        payload_json=json.dumps(
            {"actor": actor, "subject": subject, "payload": payload}, sort_keys=True
        ),
    )


__all__ = ["AUDIT_HEAD_KEY", "AuditChainEntry", "GENESIS_HASH", "build_audit_entry"]
