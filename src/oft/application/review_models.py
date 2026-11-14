"""Frozen models for the advisory LLM trade review (slice 28).

These are the typed values the review service builds and returns. They live in
their own module so ``review_service`` stays focused on orchestration. The
status discriminants are closed ``Literal`` types and each state validates its
own payload in ``__post_init__``: a FOUND result/read must carry advisory text,
while a PARTIAL/MISSING state must carry only a named reason. That keeps a
corrupt stored row from being rendered as a partial advisory answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

from oft.domain.llm_output import text_is_safe

ADVISORY_LABEL = "ADVISORY ONLY — suggests, never changes rules/prompts/risk or orders"
ReviewReadStatus = Literal["FOUND", "MISSING", "PARTIAL"]
ReviewResultStatus = Literal["FOUND", "PARTIAL"]
# The one accepted-status set, shared with the service. Drift here would turn a
# FOUND classification into an uncaught ``ValueError``/500.
VALID_REVIEW_STATUSES = frozenset({"VALID", "REPAIRED"})
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


def _valid_confidence(value: Any) -> TypeGuard[int | float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    # An arbitrary JSON int (e.g. 10**400) overflows ``float``; that is a
    # malformed value, never a 500 on the read/replay path.
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 1.0


def _utf8_encodable(value: str) -> bool:
    """Whether ``value`` can be encoded as UTF-8 (lone surrogates cannot)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _json_safe(value: Any) -> bool:
    """Whether a stored JSON value can be re-encoded without raising.

    The response layer encodes as UTF-8 and (like Starlette) rejects non-finite
    floats, so a tampered stored row must be rejected here rather than raising
    ``UnicodeEncodeError``/``ValueError`` later.
    """
    if isinstance(value, str):
        return _utf8_encodable(value)
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _utf8_encodable(key) and _json_safe(item)
            for key, item in value.items()
        )
    return False


def _has_advisory_content(
    rationale: str | None, confidence: float | None, caveats: tuple[str, ...]
) -> bool:
    return bool(rationale) or confidence is not None or bool(caveats)


def _check_found(
    rationale: str | None,
    confidence: float | None,
    validation_status: str | None,
    provenance: dict[str, Any] | None,
    reason: str | None,
) -> None:
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("a FOUND review requires a non-empty rationale")
    if not _valid_confidence(confidence):
        raise ValueError("a FOUND review requires a finite confidence within [0, 1]")
    if validation_status not in VALID_REVIEW_STATUSES:
        raise ValueError("a FOUND review requires a VALID or REPAIRED validation status")
    if not isinstance(provenance, dict) or not _json_safe(provenance):
        raise ValueError("a FOUND review requires a JSON-safe provenance object")
    if reason is not None:
        raise ValueError("a FOUND review must not carry a reason")


@dataclass(frozen=True)
class ReviewResult:
    """One persisted, advisory review decision.

    ``status`` is ``FOUND`` for an accepted review or ``PARTIAL`` for a replay of
    a tampered/incomplete stored envelope. A PARTIAL result carries a named
    ``reason`` and no advisory text, so it never reaches the response model as a
    500 or as an empty "review".
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
    status: ReviewResultStatus = "FOUND"
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.advisory:
            # A review can never be non-advisory; the flag is structural.
            raise ValueError("a review result is always advisory")
        if self.status not in {"FOUND", "PARTIAL"}:
            raise ValueError(f"unknown review result status {self.status!r}")
        if self.status == "FOUND":
            _check_found(
                self.rationale,
                self.confidence,
                self.validation_status,
                self.provenance,
                self.reason,
            )
            return
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a PARTIAL review result requires a non-empty reason")
        if _has_advisory_content(self.rationale, self.confidence, self.caveats):
            raise ValueError("a PARTIAL review result must not carry advisory content")

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
    """The provider-free read model for ``GET .../review``.

    This models an existing trade only; a missing trade raises the typed
    ``NOT_FOUND`` in :meth:`ReviewService.read`. For an existing trade every
    failure is a named ``MISSING``/``PARTIAL`` state.
    """

    trade_id: str
    status: ReviewReadStatus
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
        if self.status == "MISSING" and self.decision_id is not None:
            raise ValueError("a MISSING review read must not carry a decision id")
        if self.status == "FOUND":
            _check_found(
                self.rationale,
                self.confidence,
                self.validation_status,
                self.provenance,
                self.reason,
            )
            return
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError(f"a {self.status} review read requires a non-empty reason")
        if _has_advisory_content(self.rationale, self.confidence, self.caveats):
            raise ValueError(f"a {self.status} review read must not carry advisory content")

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
    """Return ``value`` only when it is a safe, UTF-8-encodable string, else ``None``.

    A tampered envelope may store any JSON type (or a lone surrogate/control
    character) under ``validation_status``; the API response model accepts
    ``str | None`` and responses are encoded as UTF-8, so a non-string or
    unencodable value is neutralized here instead of raising a 500.
    """
    if isinstance(value, str) and _utf8_encodable(value) and text_is_safe(value):
        return value
    return None


def validated_link_id(value: Any) -> str | None:
    """Return a usable linked review id, or ``None`` when it is malformed.

    The link lives in untrusted trade-log JSON: a non-string, blank, overlong,
    non-UTF-8, or otherwise unsafe value must never reach the store's SQL bind
    or the response layer.
    """
    if not isinstance(value, str):
        return None
    if not value.strip() or len(value) > 64:
        return None
    if not _utf8_encodable(value) or not text_is_safe(value):
        return None
    return value


def safe_identifier(value: Any) -> str | None:
    """Return ``value`` only when it is a safe, UTF-8-encodable identifier.

    Used for source ids copied from untrusted trade/opportunity payloads: an
    unsafe value is ignored exactly like a non-string, so the review proceeds
    without it rather than failing at a SQL bind or response encode.
    """
    if not isinstance(value, str) or not value:
        return None
    if not _utf8_encodable(value) or not text_is_safe(value):
        return None
    return value


def validated_review_fields(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Type- and content-check the stored review fields a response re-validates.

    The read path applies the same content invariants as the write path: an
    empty or unsafe (overlong/control/fence/embedded-instruction) rationale or
    caveat, a non-finite or out-of-range confidence, a non-UTF-8 string, or a
    provenance tree that cannot be re-encoded is malformed, not a valid review.
    """
    rationale = envelope.get("rationale")
    caveats = envelope.get("caveats")
    confidence = envelope.get("confidence")
    provenance = envelope.get("provenance")
    if not isinstance(rationale, str) or not rationale.strip():
        return None
    if not _utf8_encodable(rationale) or not text_is_safe(rationale):
        return None
    if not isinstance(caveats, list) or not all(
        isinstance(item, str) and _utf8_encodable(item) and text_is_safe(item) for item in caveats
    ):
        return None
    if not _valid_confidence(confidence):
        return None
    if not isinstance(provenance, dict) or not _json_safe(provenance):
        return None
    validation_status = validated_validation_status(envelope.get("validation_status"))
    if envelope.get("validation_status") is not None and validation_status is None:
        return None
    return {
        "rationale": rationale,
        "caveats": tuple(caveats),
        "confidence": float(confidence),
        "validation_status": validation_status,
        "provenance": provenance,
    }


__all__ = [
    "ADVISORY_LABEL",
    "VALID_REVIEW_STATUSES",
    "ReviewRead",
    "ReviewReadStatus",
    "ReviewRejected",
    "ReviewResult",
    "ReviewResultStatus",
    "safe_identifier",
    "validated_link_id",
    "validated_review_fields",
    "validated_validation_status",
]
