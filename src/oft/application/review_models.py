"""Frozen models for the advisory LLM trade review (slice 28).

These are the typed values the review service builds and returns. They live in
their own module so ``review_service`` stays focused on orchestration and under
the project's 800-line soft ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ADVISORY_LABEL = "ADVISORY ONLY — suggests, never changes rules/prompts/risk or orders"
_RETRYABLE_CODES = frozenset(
    {
        "DATA_UNAVAILABLE",
        "LLM_RATE_LIMITED",
        "LLM_TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
        "DAEMON_NOT_RUNNING",
    }
)


class ReviewRejected(Exception):
    """A typed review failure; ``code`` maps to the API error catalog."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.retryable = _RETRYABLE_CODES.__contains__(code) if retryable is None else retryable


@dataclass(frozen=True)
class ReviewResult:
    """One persisted, advisory review decision.

    A replay of a tampered envelope returns ``status="PARTIAL"`` with a named
    ``reason`` and no advisory text (the content fields stay ``None``), so a
    corrupt stored row never reaches the response model as a 500.
    """

    trade_id: str
    decision_id: str
    rationale: str | None = None
    caveats: tuple[str, ...] = ()
    confidence: float | None = None
    validation_status: str | None = None
    provenance: dict[str, Any] | None = None
    idempotent_replay: bool = False
    advisory: bool = True
    status: str = "FOUND"
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.advisory:
            # A review can never be non-advisory; the flag is structural.
            raise ValueError("a review result is always advisory")
        if not self.status:
            raise ValueError("a review result requires a status")

    def to_contract(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "decision_id": self.decision_id,
            "status": self.status,
            "advisory": True,
            "rationale": self.rationale,
            "caveats": list(self.caveats),
            "confidence": self.confidence,
            "validation_status": self.validation_status,
            "provenance": self.provenance,
            "reason": self.reason,
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class ReviewRead:
    """The provider-free read model for ``GET .../review``."""

    trade_id: str
    status: str
    decision_id: str | None = None
    rationale: str | None = None
    caveats: tuple[str, ...] = ()
    confidence: float | None = None
    validation_status: str | None = None
    provenance: dict[str, Any] | None = None
    reason: str | None = None
    advisory: bool = True

    def __post_init__(self) -> None:
        if self.status not in {"FOUND", "MISSING", "PARTIAL"}:
            raise ValueError(f"unknown review read status {self.status!r}")
        if not self.advisory:
            raise ValueError("a review read is always advisory")

    def to_contract(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "decision_id": self.decision_id,
            "status": self.status,
            "advisory": True,
            "rationale": self.rationale,
            "caveats": list(self.caveats),
            "confidence": self.confidence,
            "validation_status": self.validation_status,
            "provenance": self.provenance,
            "reason": self.reason,
        }


def validated_validation_status(value: Any) -> str | None:
    """Return ``value`` only when it is a string, else ``None``.

    A tampered envelope may store any JSON type under ``validation_status``;
    the API response model accepts ``str | None`` only, so a non-string is
    neutralized here instead of letting response validation raise a 500.
    """
    return value if isinstance(value, str) else None


def validated_review_fields(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Type-check the stored review fields a response model will re-validate."""
    rationale = envelope.get("rationale")
    caveats = envelope.get("caveats")
    confidence = envelope.get("confidence")
    provenance = envelope.get("provenance")
    if not isinstance(rationale, str):
        return None
    if not isinstance(caveats, list) or not all(isinstance(item, str) for item in caveats):
        return None
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        return None
    if not isinstance(provenance, dict):
        return None
    return {
        "rationale": rationale,
        "caveats": tuple(caveats),
        "confidence": confidence,
        "validation_status": validated_validation_status(envelope.get("validation_status")),
        "provenance": provenance,
    }


__all__ = [
    "ADVISORY_LABEL",
    "ReviewRead",
    "ReviewRejected",
    "ReviewResult",
    "validated_review_fields",
    "validated_validation_status",
]
