"""Canonical JSON Schema -> Pydantic -> semantic validation with one repair.

The canonical provider-facing schema is ``docs/contracts/llm-output.schema.json``
(Draft 2020-12). A markdown fence, empty body, or truncated JSON is a failure;
no fuzzy parsing or "best effort" coercion is performed (ADR-0005).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError

from oft.domain.llm_output import DecisionOutput, SemanticError, semantic_errors

_CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
# Reject an oversized body before parsing: an untrusted provider (or a future
# real adapter) must not be able to force an unbounded allocation.
MAX_RAW_OUTPUT_CHARS = 256_000


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str

    def to_contract(self) -> dict[str, Any]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ValidationOutcome:
    status: str
    output: DecisionOutput | None
    errors: tuple[ValidationIssue, ...]
    raw_output_hash: str
    repaired: bool = False

    @property
    def valid(self) -> bool:
        return self.status in {"VALID", "REPAIRED"} and self.output is not None


@lru_cache(maxsize=1)
def canonical_schema() -> dict[str, Any]:
    document: Any = json.loads((_CONTRACTS / "llm-output.schema.json").read_text(encoding="utf-8"))
    return document  # type: ignore[no-any-return]


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(canonical_schema())


def schema_text() -> str:
    """The canonical schema as compact text for the prompt."""
    return json.dumps(canonical_schema(), sort_keys=True, separators=(",", ":"))


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema_issues(document: Any) -> list[ValidationIssue]:
    errors = sorted(_validator().iter_errors(document), key=lambda error: list(error.path))
    return [
        ValidationIssue(
            field=".".join(str(part) for part in error.path) or "output",
            code="SCHEMA_INVALID",
            message=error.message[:300],
        )
        for error in errors
    ]


def _pydantic_issues(exc: ValidationError) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for error in exc.errors():
        issues.append(
            ValidationIssue(
                field=".".join(str(part) for part in error.get("loc", ())) or "output",
                code="PYDANTIC_INVALID",
                message=str(error.get("msg", "invalid value"))[:300],
            )
        )
    return issues


def _semantic_issues(output: DecisionOutput, role: str) -> list[ValidationIssue]:
    return [
        ValidationIssue(field=error.field, code=error.code, message=error.message)
        for error in semantic_errors(output, role=role)
    ]


def _validate_once(
    raw_text: str, *, role: str
) -> tuple[DecisionOutput | None, list[ValidationIssue]]:
    if len(raw_text) > MAX_RAW_OUTPUT_CHARS:
        return None, [
            ValidationIssue(
                "output",
                "LLM_SCHEMA_INVALID",
                f"the provider output exceeds {MAX_RAW_OUTPUT_CHARS} characters",
            )
        ]
    text = raw_text.strip()
    if not text:
        return None, [
            ValidationIssue("output", "LLM_OUTPUT_TRUNCATED", "the provider returned an empty body")
        ]
    if text.startswith("```") or "```" in text:
        return None, [
            ValidationIssue(
                "output",
                "LLM_SCHEMA_INVALID",
                "markdown fences are not valid provider output",
            )
        ]
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError):
        return None, [
            ValidationIssue("output", "LLM_SCHEMA_INVALID", "the output is not valid JSON")
        ]
    issues = _schema_issues(document)
    if issues:
        return None, issues
    try:
        output = DecisionOutput.model_validate(document)
    except ValidationError as exc:
        return None, _pydantic_issues(exc)
    semantic = _semantic_issues(output, role)
    if semantic:
        return None, semantic
    return output, []


def validate_output(
    raw_text: str,
    *,
    role: str,
    repair: Callable[[str], str] | None = None,
) -> ValidationOutcome:
    """Validate one provider output, allowing exactly one repair attempt."""
    raw_hash = hash_text(raw_text)
    output, issues = _validate_once(raw_text, role=role)
    if output is not None:
        return ValidationOutcome(
            status="VALID",
            output=output,
            errors=(),
            raw_output_hash=raw_hash,
        )
    if repair is None:
        return ValidationOutcome(
            status="REJECTED",
            output=None,
            errors=tuple(issues),
            raw_output_hash=raw_hash,
        )
    message = issues[0].message if issues else "invalid output"
    repaired_raw = repair(message)
    repaired_output, repaired_issues = _validate_once(repaired_raw, role=role)
    if repaired_output is not None:
        return ValidationOutcome(
            status="REPAIRED",
            output=repaired_output,
            errors=(),
            raw_output_hash=hash_text(repaired_raw),
            repaired=True,
        )
    return ValidationOutcome(
        status="REJECTED",
        output=None,
        errors=tuple(repaired_issues),
        raw_output_hash=hash_text(repaired_raw),
        repaired=True,
    )


def decision_envelope(
    *,
    decision_id: str,
    role: str,
    output: DecisionOutput | None,
    subject: dict[str, Any],
    provenance: dict[str, Any],
    validation_status: str,
    validation_errors: tuple[ValidationIssue, ...],
    rejected_reason: str | None,
    raw_output_hash: str,
    persisted_at: str,
    decision_type: str | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    caveats: list[str] | None = None,
    analysis: dict[str, Any] | None = None,
    opportunity: dict[str, Any] | None = None,
    entry_recheck: dict[str, Any] | None = None,
    position_management: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the persisted ``llm-decision`` envelope; provider provenance is ignored."""
    document = output.model_dump(mode="json") if output is not None else {}
    return {
        "schema_version": "1.0.0",
        "decision_id": decision_id,
        "decision_type": decision_type or document.get("decision_type", "MARKET_ANALYSIS"),
        "role": role,
        "subject": subject,
        "provenance": provenance,
        "confidence": confidence if confidence is not None else document.get("confidence", 0.0),
        "rationale": rationale if rationale is not None else document.get("rationale", ""),
        "caveats": caveats if caveats is not None else document.get("caveats", []),
        "analysis": analysis if analysis is not None else document.get("analysis"),
        "opportunity": opportunity if opportunity is not None else document.get("opportunity"),
        "entry_recheck": (
            entry_recheck if entry_recheck is not None else document.get("entry_recheck")
        ),
        "position_management": (
            position_management
            if position_management is not None
            else document.get("position_management")
        ),
        "validation_status": validation_status,
        "validation_errors": [issue.to_contract() for issue in validation_errors],
        "rejected_reason": rejected_reason,
        "raw_output_hash": raw_output_hash,
        "persisted_at": persisted_at,
    }


__all__ = [
    "SemanticError",
    "ValidationIssue",
    "ValidationOutcome",
    "canonical_schema",
    "decision_envelope",
    "hash_text",
    "schema_text",
    "validate_output",
]
