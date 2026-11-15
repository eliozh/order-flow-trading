"""Pure LLM provider/model settings rules (slice 35).

One active provider, four role models, the official base URLs, the custom-host
opt-in, and the concurrency bounds live here so the service, API, CLI, and Web
client share one vocabulary. The mask is deliberately a fixed prefix plus the
last four characters: a key's provider prefix, length, and value are never
derivable from a returned artifact.

No temperature, timeout, per-role provider, or model-discovery field exists:
the role table is provider-agnostic and the active provider applies to every
role. Provider SDK semantics are unchanged.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from oft.domain.settings import SettingValidationError

LLM_SETTINGS_KEY = "llm.settings"
SECRET_NAME = "api_key"

PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "deepseek")
ROLE_NAMES: tuple[str, ...] = ("ANALYSIS", "ENTRY_RECHECK", "MANAGEMENT", "REVIEW")
ROLE_MODEL_FIELDS: Mapping[str, str] = {
    "ANALYSIS": "llm_model_analysis",
    "ENTRY_RECHECK": "llm_model_entry_recheck",
    "MANAGEMENT": "llm_model_management",
    "REVIEW": "llm_model_review",
}
PROVIDER_KEY_FIELDS: Mapping[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
}
DEFAULT_BASE_URLS: Mapping[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
}
OFFICIAL_HOSTS: Mapping[str, frozenset[str]] = {
    "openai": frozenset({"api.openai.com"}),
    "anthropic": frozenset({"api.anthropic.com"}),
    "deepseek": frozenset({"api.deepseek.com"}),
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

MASK_PREFIX = "****"
# A realistic provider key is far longer than four characters; the floor also
# guarantees the rendered last-4 can never be the whole accepted value.
MIN_API_KEY_LENGTH = 16
# Bound the payload so a caller cannot force an arbitrarily large body read,
# SHA-256, encryption, and fsync with one accepted request.
MAX_API_KEY_LENGTH = 512
MAX_CONCURRENCY = 64
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_CONCURRENCY_DEEPSEEK = 1

SECRET_ADDED_EVENT = "SECRET_ADDED"
SECRET_ROTATED_EVENT = "SECRET_ROTATED"
SECRET_DELETED_EVENT = "SECRET_DELETED"
SECRET_SUBJECT_KIND = "SECRET"


class LLMSettingsValidationError(SettingValidationError):
    """A provider/model/base-URL/key value failed typed validation."""


def _detail(field: str | None, code: str, message: str) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message}


def mask_secret(value: str) -> str:
    """The only renderable form of a key: ``****`` plus the last four chars."""
    if len(value) < MIN_API_KEY_LENGTH:
        return MASK_PREFIX
    return f"{MASK_PREFIX}{value[-4:]}"


def last4(value: str) -> str:
    """The stored last-four suffix (empty when the value is too short)."""
    return value[-4:] if len(value) >= MIN_API_KEY_LENGTH else ""


def fingerprint_secret(value: str) -> str:
    """A salted-free SHA-256 fingerprint used to detect a rotation."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_api_key(value: object) -> str:
    """Normalize one provider key; never echo the value in an error."""
    if not isinstance(value, str):
        raise LLMSettingsValidationError(
            "api_key must be a string",
            details=[_detail("api_key", "not_a_string", "api_key must be a string")],
        )
    normalized = value.strip()
    if len(normalized) < MIN_API_KEY_LENGTH:
        message = f"api_key must be at least {MIN_API_KEY_LENGTH} non-space characters"
        raise LLMSettingsValidationError(
            message, details=[_detail("api_key", "too_short", message)]
        )
    if len(normalized) > MAX_API_KEY_LENGTH:
        message = f"api_key must be at most {MAX_API_KEY_LENGTH} non-space characters"
        raise LLMSettingsValidationError(message, details=[_detail("api_key", "too_long", message)])
    if any(
        unicodedata.category(character).startswith("C") or character.isspace()
        for character in normalized
    ):
        message = "api_key must not contain whitespace or control characters"
        raise LLMSettingsValidationError(
            message, details=[_detail("api_key", "invalid_characters", message)]
        )
    return normalized


def validate_provider(value: object) -> str | None:
    """Return the normalized active provider, or ``None`` when unset."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMSettingsValidationError(
            "provider must be a string or null",
            details=[_detail("provider", "invalid_type", "provider must be a string or null")],
        )
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in PROVIDERS:
        message = f"provider must be one of: {', '.join(PROVIDERS)}"
        raise LLMSettingsValidationError(
            message, details=[_detail("provider", "unsupported_provider", message)]
        )
    return normalized


def validate_roles(value: object) -> dict[str, str | None]:
    """Return the four role models in canonical order; unknown roles fail."""
    if value is None:
        return {role: None for role in ROLE_NAMES}
    if not isinstance(value, Mapping):
        raise LLMSettingsValidationError(
            "roles must be an object",
            details=[_detail("roles", "not_an_object", "roles must be an object")],
        )
    unknown = sorted(str(key) for key in value if str(key) not in ROLE_NAMES)
    if unknown:
        message = "unknown role(s): " + ", ".join(unknown)
        raise LLMSettingsValidationError(
            message,
            details=[
                _detail(f"roles.{name}", "unknown_role", f"unknown role {name}") for name in unknown
            ],
        )
    normalized: dict[str, str | None] = {}
    for role in ROLE_NAMES:
        raw = value.get(role)
        if raw is None:
            normalized[role] = None
            continue
        if not isinstance(raw, str):
            message = f"roles.{role} must be a string or null"
            raise LLMSettingsValidationError(
                message,
                details=[_detail(f"roles.{role}", "invalid_type", message)],
            )
        model = raw.strip()
        if not model:
            normalized[role] = None
            continue
        if len(model) > 200 or any(character.isspace() for character in model):
            message = f"roles.{role} must be a model id without whitespace"
            raise LLMSettingsValidationError(
                message,
                details=[_detail(f"roles.{role}", "invalid_model", message)],
            )
        normalized[role] = model
    return normalized


def validate_concurrency(value: object) -> int:
    """A bounded positive integer concurrency limit."""
    if isinstance(value, bool) or not isinstance(value, int):
        message = "concurrency must be an integer"
        raise LLMSettingsValidationError(
            message, details=[_detail("concurrency", "invalid_type", message)]
        )
    if value < 1 or value > MAX_CONCURRENCY:
        message = f"concurrency must be between 1 and {MAX_CONCURRENCY}"
        raise LLMSettingsValidationError(
            message, details=[_detail("concurrency", "out_of_range", message)]
        )
    return value


def validate_base_url(value: object, *, provider: str, allow_custom: bool) -> str:
    """Validate one base URL, naming the rule and the provider-scoped field.

    https is mandatory except for loopback. Userinfo, a query, and a fragment
    are always rejected: all three can redirect or re-target the key. A
    non-official host needs the explicit opt-in.
    """
    field = f"base_urls.{provider}"

    def fail(code: str, message: str) -> LLMSettingsValidationError:
        return LLMSettingsValidationError(message, details=[_detail(field, code, message)])

    if not isinstance(value, str):
        raise fail("invalid_type", f"{field} must be a string")
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise fail("empty", f"{field} must not be empty")
    try:
        parts = urlsplit(normalized)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise fail("invalid_url", f"{field} is not a valid URL") from exc
    del port
    if not host:
        raise fail("missing_host", f"{field} must include a host")
    if parts.username or parts.password:
        raise fail("userinfo_forbidden", f"{field} must not contain userinfo")
    if parts.query:
        raise fail("query_forbidden", f"{field} must not contain a query")
    if parts.fragment:
        raise fail("fragment_forbidden", f"{field} must not contain a fragment")
    scheme = parts.scheme.lower()
    if scheme == "http" and host in LOOPBACK_HOSTS:
        return normalized
    if scheme != "https":
        raise fail(
            "https_required",
            f"{field} must use https; http is allowed only for loopback",
        )
    if host in OFFICIAL_HOSTS.get(provider, frozenset()) or host in LOOPBACK_HOSTS:
        return normalized
    if allow_custom:
        return normalized
    raise fail(
        "unofficial_host",
        f"{field} must use the official {provider} host or loopback; "
        "set the custom-base-URL opt-in to allow another host",
    )


__all__ = [
    "DEFAULT_BASE_URLS",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_CONCURRENCY_DEEPSEEK",
    "LLMSettingsValidationError",
    "LLM_SETTINGS_KEY",
    "LOOPBACK_HOSTS",
    "MASK_PREFIX",
    "MAX_API_KEY_LENGTH",
    "MAX_CONCURRENCY",
    "MIN_API_KEY_LENGTH",
    "OFFICIAL_HOSTS",
    "PROVIDERS",
    "PROVIDER_KEY_FIELDS",
    "ROLE_MODEL_FIELDS",
    "ROLE_NAMES",
    "SECRET_ADDED_EVENT",
    "SECRET_DELETED_EVENT",
    "SECRET_NAME",
    "SECRET_ROTATED_EVENT",
    "SECRET_SUBJECT_KIND",
    "fingerprint_secret",
    "last4",
    "mask_secret",
    "validate_api_key",
    "validate_base_url",
    "validate_concurrency",
    "validate_provider",
    "validate_roles",
]
