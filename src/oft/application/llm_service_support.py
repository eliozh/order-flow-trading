"""Behavior-identical helpers shared by the review and management services.

The advisory review (slice 28) and position-management services both bound an
untrusted history block, map validator codes to the LLM error vocabulary, save a
schema-validated ``llm-decision`` envelope, build the provider provenance block,
and resolve the requested provider the same way. Keeping those implementations
in one module means the two paths cannot drift.

This is a low-level helpers module: it deliberately imports no execution,
management, position, broker, or gateway code, only the store/LLM ports and the
orchestrator's ``provider_configured`` helper.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from oft.application.llm_orchestrator import provider_configured
from oft.ports.store import DecisionRecord, StorePort

BOUNDED_HISTORY_MAX_ENTRIES = 20
BOUNDED_HISTORY_TEXT_LIMIT = 200
BOUNDED_HISTORY_MAX_BYTES = 4096

_LLM_ERROR_CODES = frozenset({"LLM_REFUSED", "LLM_OUTPUT_TRUNCATED", "LLM_SCHEMA_INVALID"})


class ProviderSelectionError(Exception):
    """A typed provider-selection failure each service maps to its own exception."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def bounded_history(
    value: object,
    *,
    max_entries: int = BOUNDED_HISTORY_MAX_ENTRIES,
    text_limit: int = BOUNDED_HISTORY_TEXT_LIMIT,
    max_bytes: int = BOUNDED_HISTORY_MAX_BYTES,
) -> list[dict[str, Any]]:
    """The newest bounded management-history entries for the prompt.

    Keeps at most ``max_entries`` newest dict entries, truncates each free-text
    note/rationale, and drops the oldest entries until the serialized form fits
    ``max_bytes``. Non-dict entries (tampered or legacy) are dropped, but the
    newest-``max_entries`` slice happens first, so they can consume slots in
    that window rather than being skipped beforehand.
    """
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value[-max_entries:]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        for key in ("note", "rationale"):
            text = entry.get(key)
            if isinstance(text, str) and len(text) > text_limit:
                entry[key] = text[:text_limit]
        entries.append(entry)
    while entries and (len(json.dumps(entries, sort_keys=True).encode("utf-8")) > max_bytes):
        entries.pop(0)
    return entries


def llm_error_code(code: str | None) -> str:
    """Map a validator code to the error catalog's LLM vocabulary."""
    return code if code in _LLM_ERROR_CODES else "LLM_SCHEMA_INVALID"


def save_llm_decision(
    store: StorePort,
    *,
    decision_id: str,
    role: str,
    decision_type: str,
    validation_status: str,
    raw_output_hash: str,
    envelope: dict[str, Any],
    now: str,
) -> None:
    """Persist one decision envelope with its code-owned identity fields."""
    store.save_decision(
        DecisionRecord(
            decision_id=decision_id,
            role=role,
            decision_type=decision_type,
            validation_status=validation_status,
            raw_output_hash=raw_output_hash,
            persisted_at=now,
            payload_json=json.dumps(envelope, sort_keys=True, allow_nan=False),
        )
    )


def decision_provenance(
    *,
    provider: Any,
    model: Any,
    prompt_id: Any,
    prompt_version: Any,
    prompt_hash: Any,
    knowledge_artifact_hash: Any,
    knowledge_status: Any,
    knowledge_reason: Any,
    input_hash: str,
    request_id: str,
    latency_ms: Any,
    usage: Any,
    amount_usd: Any,
    price_version: Any,
    unpriced: Any,
    recorded_at: str,
    due_conditions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The persisted provenance block, identical for both advisory services.

    ``due_conditions`` is omitted entirely when ``None`` so the review envelope
    stays byte-identical to its historical shape; the management service passes
    the normalized condition list exactly as before.
    """
    provenance: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "model_snapshot": None,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "knowledge_artifact_hash": knowledge_artifact_hash,
        "knowledge_status": knowledge_status,
        "knowledge_reason": knowledge_reason,
        "input_hash": input_hash,
        "schema_version": "1.0.0",
        "temperature": 0.0,
        "seed": None,
        "request_id": request_id,
        "latency_ms": latency_ms,
        "usage": usage,
        "cost": {
            "amount_usd": amount_usd,
            "currency": "USD",
            "price_version": price_version,
            "unpriced": unpriced,
        },
        "recorded_at": recorded_at,
    }
    if due_conditions is not None:
        provenance["due_conditions"] = list(due_conditions)
    return provenance


def resolve_provider_name(
    provider: Any,
    settings: Any,
    requested: str | None,
    *,
    role_label: str,
    run_label: str | None = None,
) -> str:
    """Resolve and validate the provider for one advisory role.

    Raises :class:`ProviderSelectionError` with the code/message each service
    has historically exposed; the caller wraps it into its own rejected type.
    ``role_label`` names the role ("for the review role"); ``run_label`` names
    the action in the "cannot run" sentences and defaults to ``role_label``.
    """
    cannot_run = role_label if run_label is None else run_label
    value = (requested or "").strip().lower()
    if provider is None:
        raise ProviderSelectionError(
            "LLM_PROVIDER_NOT_CONFIGURED",
            f"No LLM provider is configured; the {cannot_run} cannot run.",
        )
    name = value or str(getattr(provider, "provider", "") or "")
    if not name:
        capabilities = getattr(provider, "capabilities", None)
        if callable(capabilities):
            name = str(getattr(capabilities(), "provider", "") or "")
    if requested and name != requested.strip().lower():
        raise ProviderSelectionError(
            "LLM_PROVIDER_NOT_CONFIGURED",
            f"No {requested} provider is configured for the {role_label} role.",
        )
    if not provider_configured(settings, name):
        raise ProviderSelectionError(
            "LLM_PROVIDER_NOT_CONFIGURED",
            f"No {name or 'LLM'} API key is configured; the {cannot_run} cannot run.",
        )
    return name


__all__ = [
    "BOUNDED_HISTORY_MAX_BYTES",
    "BOUNDED_HISTORY_MAX_ENTRIES",
    "BOUNDED_HISTORY_TEXT_LIMIT",
    "ProviderSelectionError",
    "bounded_history",
    "decision_provenance",
    "llm_error_code",
    "resolve_provider_name",
    "save_llm_decision",
]
