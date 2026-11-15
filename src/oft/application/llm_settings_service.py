"""LLM provider/model settings and the runtime effective-settings overlay.

Extracted from :mod:`oft.application.settings_service` so scan/risk and
LLM/secret concerns stay separate files. ``SettingsService`` inherits from
:class:`LLMSettingsMixin`; the public API is unchanged.

The persisted document is non-secret only. Environment values are read from the
pristine pre-overlay settings the mixin was constructed with, never from the
mutable runtime overlay.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from oft.application.llm_orchestrator import capability_payload
from oft.application.secrets_service import SecretsService, SecretStoreUnavailable
from oft.config import ConfigError
from oft.domain.llm_settings import (
    DEFAULT_BASE_URLS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_CONCURRENCY_DEEPSEEK,
    LLM_SETTINGS_KEY,
    PROVIDER_KEY_FIELDS,
    PROVIDERS,
    ROLE_MODEL_FIELDS,
    ROLE_NAMES,
    validate_base_url,
    validate_concurrency,
    validate_provider,
    validate_roles,
)
from oft.domain.settings import (
    CONFIG_SUBJECT_KIND,
    CONFIG_UPDATED_EVENT,
    SettingSource,
    SettingValidationError,
    scoped_diff,
)
from oft.domain.time import utc_timestamp
from oft.ports.store import AuditEventDraft, ConfigRecord

if TYPE_CHECKING:
    from oft.config import Settings

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMSettings:
    """The effective LLM provider document plus write-only key metadata."""

    provider: str | None
    roles: dict[str, str | None]
    base_urls: dict[str, str]
    allow_custom_base_url: bool
    max_concurrency: int
    max_concurrency_deepseek: int
    source: SettingSource
    updated_at: str | None
    updated_by: str | None
    providers: tuple[dict[str, Any], ...] = ()

    def document(self) -> dict[str, Any]:
        """The non-secret effective document persisted under ``llm.settings``."""
        return {
            "provider": self.provider,
            "roles": dict(self.roles),
            "base_urls": dict(self.base_urls),
            "allow_custom_base_url": self.allow_custom_base_url,
            "max_concurrency": self.max_concurrency,
            "max_concurrency_deepseek": self.max_concurrency_deepseek,
        }

    def to_contract(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "providers": [dict(row) for row in self.providers],
            "roles": dict(self.roles),
            "base_urls": dict(self.base_urls),
            "allow_custom_base_url": self.allow_custom_base_url,
            "concurrency": {
                "global": self.max_concurrency,
                "deepseek": self.max_concurrency_deepseek,
            },
            "source": self.source.value,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class LLMSettingsMixin:
    """The LLM half of ``SettingsService`` (requires its store/clock/settings)."""

    _store: Any
    _clock: Any
    _settings: Settings | None

    # -- environment baseline (pristine, never the overlay) ---------------

    def _secrets(self) -> SecretsService:
        return SecretsService(store=self._store, settings=self._settings, clock=self._clock)

    def _env_base_url(self, provider: str) -> str:
        if self._settings is None:
            return DEFAULT_BASE_URLS[provider]
        attribute = {
            "openai": "llm_openai_base_url",
            "anthropic": "llm_anthropic_base_url",
            "deepseek": "llm_deepseek_base_url",
        }[provider]
        return str(getattr(self._settings, attribute, DEFAULT_BASE_URLS[provider]))

    def _env_max_concurrency(self, provider: str = "openai") -> int:
        if self._settings is None:
            return (
                DEFAULT_MAX_CONCURRENCY_DEEPSEEK
                if provider == "deepseek"
                else DEFAULT_MAX_CONCURRENCY
            )
        if provider == "deepseek":
            return int(self._settings.llm_max_concurrency_deepseek)
        return int(self._settings.llm_max_concurrency)

    def _env_provider(self) -> str | None:
        if self._settings is None:
            return None
        raw = self._settings.llm_provider
        if raw is None:
            return None
        # ``stub`` is an environment-only test transport. It must never enter
        # the persisted document or the settings response; the runtime overlay
        # keeps it separately (see ``resolve_effective_llm_settings``).
        if raw.strip().lower() == "stub":
            return None
        return validate_provider(raw)

    def _env_allow_custom(self) -> bool:
        return (
            bool(self._settings.llm_allow_custom_base_url) if self._settings is not None else False
        )

    def _env_roles(self) -> dict[str, str | None]:
        if self._settings is None:
            return {role: None for role in ROLE_NAMES}
        return {role: self._settings.model_for_role(role) for role in ROLE_NAMES}

    def _env_llm_document(self) -> dict[str, Any]:
        return {
            "provider": self._env_provider(),
            "roles": self._env_roles(),
            "base_urls": {provider: self._env_base_url(provider) for provider in PROVIDERS},
            "allow_custom_base_url": self._env_allow_custom(),
            "max_concurrency": self._env_max_concurrency(),
            "max_concurrency_deepseek": self._env_max_concurrency("deepseek"),
        }

    # -- decode / validate ------------------------------------------------

    def _decode_llm(self, record: ConfigRecord) -> dict[str, Any]:
        """Decode and validate a persisted LLM document; fail closed."""
        try:
            value = json.loads(record.value_json)
        except (TypeError, ValueError) as exc:
            raise ConfigError("persisted llm.settings is unreadable") from exc
        if not isinstance(value, dict):
            raise ConfigError("persisted llm.settings is not an object")
        try:
            decoded = self._normalize_document(value)
        except SettingValidationError as exc:
            raise ConfigError(f"persisted llm.settings is invalid: {exc.message}") from exc
        return decoded

    def _normalize_document(self, value: Mapping[str, Any]) -> dict[str, Any]:
        provider = validate_provider(value.get("provider"))
        roles = validate_roles(value.get("roles", {}))
        raw_allow_custom = value.get("allow_custom_base_url", False)
        if not isinstance(raw_allow_custom, bool):
            raise SettingValidationError(
                "allow_custom_base_url must be a boolean",
                details=[
                    {
                        "field": "allow_custom_base_url",
                        "code": "invalid_type",
                        "message": "allow_custom_base_url must be a boolean",
                    }
                ],
            )
        base_urls = self._validate_document_base_urls(
            value.get("base_urls", {}), allow_custom=raw_allow_custom
        )
        global_limit = validate_concurrency(
            value.get("max_concurrency", self._env_max_concurrency())
        )
        deepseek_limit = validate_concurrency(
            value.get("max_concurrency_deepseek", self._env_max_concurrency("deepseek"))
        )
        return {
            "provider": provider,
            "roles": roles,
            "base_urls": base_urls,
            "allow_custom_base_url": raw_allow_custom,
            "max_concurrency": global_limit,
            "max_concurrency_deepseek": deepseek_limit,
        }

    def _validate_document_base_urls(
        self, base_urls: object, *, allow_custom: bool
    ) -> dict[str, str]:
        if not isinstance(base_urls, Mapping):
            raise SettingValidationError(
                "base_urls must be an object",
                details=[
                    {
                        "field": "base_urls",
                        "code": "not_an_object",
                        "message": "base_urls must be an object",
                    }
                ],
            )
        validated: dict[str, str] = {}
        for provider in PROVIDERS:
            raw = base_urls.get(provider, self._env_base_url(provider))
            validated[provider] = validate_base_url(
                raw, provider=provider, allow_custom=allow_custom
            )
        return validated

    # -- reads -----------------------------------------------------------

    def read_effective_llm(self) -> LLMSettings:
        """The persisted document overlaid field-by-field on the environment."""
        record = self._store.get_config(LLM_SETTINGS_KEY)
        if record is None:
            document = self._env_llm_document()
            source = SettingSource.DEFAULT
            updated_at: str | None = None
            updated_by: str | None = None
        else:
            document = self._decode_llm(record)
            source = SettingSource.USER
            updated_at = record.updated_at
            updated_by = record.updated_by
        return LLMSettings(
            provider=document["provider"],
            roles=dict(document["roles"]),
            base_urls=dict(document["base_urls"]),
            allow_custom_base_url=bool(document["allow_custom_base_url"]),
            max_concurrency=int(document["max_concurrency"]),
            max_concurrency_deepseek=int(document["max_concurrency_deepseek"]),
            source=source,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def read_llm_settings(self) -> LLMSettings:
        """The effective document plus masked provider rows and test status."""
        effective = self.read_effective_llm()
        secrets = self._secrets()
        rows: list[dict[str, Any]] = []
        for provider in PROVIDERS:
            status = secrets.status(provider)
            row = status.to_contract()
            row["base_url"] = effective.base_urls[provider]
            row["capability"] = capability_payload(self._store.get_provider_capability(provider))
            rows.append(row)
        return replace(effective, providers=tuple(rows))

    # -- writes ----------------------------------------------------------

    def update_llm_settings(
        self,
        changes: Mapping[str, Any],
        *,
        actor: str,
        reason: str = "",
    ) -> LLMSettings:
        """Validate a partial LLM document and persist one audited change.

        The patch is merged into the document read *inside* the write
        transaction, so two concurrent partial saves cannot lose each other's
        field or record a stale ``before``. The complete final document is
        validated with the same rules the read path uses before it is
        committed; a save that changes nothing writes no row and no audit.
        """
        if not isinstance(changes, Mapping):
            raise SettingValidationError(
                "llm settings must be an object",
                details=[
                    {
                        "field": None,
                        "code": "not_an_object",
                        "message": "llm settings must be an object",
                    }
                ],
            )
        timestamp = utc_timestamp(self._clock.now())

        def _build(record: ConfigRecord | None) -> tuple[ConfigRecord, AuditEventDraft] | None:
            before = self._decode_llm(record) if record is not None else self._env_llm_document()
            proposed = self._merge_llm_document(before, changes)
            if proposed == before:
                return None
            config = ConfigRecord(
                key=LLM_SETTINGS_KEY,
                value_json=json.dumps(proposed, sort_keys=True),
                updated_at=timestamp,
                updated_by=actor,
            )
            audit = AuditEventDraft(
                event_type=CONFIG_UPDATED_EVENT,
                actor={"kind": "USER", "id": actor},
                subject={"kind": CONFIG_SUBJECT_KIND, "id": LLM_SETTINGS_KEY},
                payload={
                    **scoped_diff(key=LLM_SETTINGS_KEY, before=before, after=proposed),
                    "source_before": (
                        SettingSource.USER if record is not None else SettingSource.DEFAULT
                    ).value,
                    "source_after": SettingSource.USER.value,
                    "reason": reason,
                },
                created_at=timestamp,
            )
            return config, audit

        self._store.save_config_change(LLM_SETTINGS_KEY, _build)
        return self.read_llm_settings()

    def _merge_llm_document(
        self, current: Mapping[str, Any], changes: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "provider",
            "roles",
            "base_urls",
            "allow_custom_base_url",
            "max_concurrency",
            "max_concurrency_deepseek",
        }
        unknown = sorted(str(key) for key in changes if key not in allowed)
        if unknown:
            raise SettingValidationError(
                "unknown llm setting(s): " + ", ".join(unknown),
                details=[
                    {
                        "field": name,
                        "code": "unknown_field",
                        "message": f"unknown llm setting {name}",
                    }
                    for name in unknown
                ],
            )
        proposed: dict[str, Any] = {
            **dict(current),
            "roles": dict(current["roles"]),
            "base_urls": dict(current["base_urls"]),
        }
        if "provider" in changes:
            proposed["provider"] = validate_provider(changes["provider"])
        if "roles" in changes:
            patch = changes["roles"]
            if not isinstance(patch, Mapping):
                raise SettingValidationError(
                    "roles must be an object",
                    details=[
                        {
                            "field": "roles",
                            "code": "not_an_object",
                            "message": "roles must be an object",
                        }
                    ],
                )
            validated = validate_roles(patch)
            roles = dict(proposed["roles"])
            for role in patch:
                roles[str(role)] = validated[str(role)]
            proposed["roles"] = roles
        if "allow_custom_base_url" in changes:
            value = changes["allow_custom_base_url"]
            if not isinstance(value, bool):
                raise SettingValidationError(
                    "allow_custom_base_url must be a boolean",
                    details=[
                        {
                            "field": "allow_custom_base_url",
                            "code": "invalid_type",
                            "message": "allow_custom_base_url must be a boolean",
                        }
                    ],
                )
            proposed["allow_custom_base_url"] = value
        if "base_urls" in changes:
            patch = changes["base_urls"]
            if not isinstance(patch, Mapping):
                raise SettingValidationError(
                    "base_urls must be an object",
                    details=[
                        {
                            "field": "base_urls",
                            "code": "not_an_object",
                            "message": "base_urls must be an object",
                        }
                    ],
                )
            unknown_urls = sorted(str(key) for key in patch if key not in PROVIDERS)
            if unknown_urls:
                raise SettingValidationError(
                    "unknown base_url provider(s): " + ", ".join(unknown_urls),
                    details=[
                        {
                            "field": f"base_urls.{name}",
                            "code": "unknown_provider",
                            "message": f"unknown provider {name}",
                        }
                        for name in unknown_urls
                    ],
                )
            base_urls = dict(proposed["base_urls"])
            for provider in patch:
                base_urls[provider] = patch[provider]
            proposed["base_urls"] = base_urls
        if "max_concurrency" in changes:
            proposed["max_concurrency"] = validate_concurrency(changes["max_concurrency"])
        if "max_concurrency_deepseek" in changes:
            proposed["max_concurrency_deepseek"] = validate_concurrency(
                changes["max_concurrency_deepseek"]
            )
        # The same validator the read path uses, applied to the complete final
        # document. Disabling the custom-host opt-in while any non-official URL
        # remains is rejected here, before anything is committed.
        proposed["base_urls"] = self._validate_document_base_urls(
            proposed["base_urls"],
            allow_custom=bool(proposed["allow_custom_base_url"]),
        )
        return proposed


def resolve_effective_llm_settings(*, settings: Settings, store: Any, clock: Any) -> Settings:
    """A :class:`Settings` copy with the persisted LLM overlay and stored keys.

    Called at provider-resolution time (not only at startup), so a save through
    the API or CLI applies to the next scan/entry/management/review/probe call
    without a restart. Only LLM fields are changed; every other setting keeps
    its environment value.
    """
    service = _Service(store=store, clock=clock, settings=settings)
    effective = service.read_effective_llm()
    secrets = service._secrets()
    provider_value = effective.provider
    # ``stub`` is an environment-only transport. Keep it for the runtime
    # whenever the effective persisted provider is null, regardless of whether
    # an operator document exists: an unrelated save (e.g. concurrency) must
    # not silently drop the stub transport, and a restart must resolve it the
    # same way. The settings surface still reports ``provider: null`` because
    # ``stub`` is never persisted.
    if provider_value is None:
        base_provider = (settings.llm_provider or "").strip().lower()
        if base_provider == "stub":
            provider_value = "stub"
    update: dict[str, Any] = {
        "llm_provider": provider_value,
        "llm_allow_custom_base_url": effective.allow_custom_base_url,
        "llm_openai_base_url": effective.base_urls["openai"],
        "llm_anthropic_base_url": effective.base_urls["anthropic"],
        "llm_deepseek_base_url": effective.base_urls["deepseek"],
        "llm_max_concurrency": effective.max_concurrency,
        "llm_max_concurrency_deepseek": effective.max_concurrency_deepseek,
    }
    for role, attribute in ROLE_MODEL_FIELDS.items():
        update[attribute] = effective.roles[role]
    for provider in PROVIDERS:
        try:
            key = secrets.resolve_key(provider)
        except SecretStoreUnavailable:
            # A metadata row whose store is unavailable is surfaced as an
            # explicit ``unavailable`` state on the settings API; the runtime
            # simply has no stored key to install (the env key, if any, stays).
            _LOGGER.error("llm_secret_store_unavailable provider=%s", provider)
            key = None
        if key is not None:
            update[PROVIDER_KEY_FIELDS[provider]] = SecretStr(key)
    return settings.model_copy(update=update)


def _Service(*, store: Any, clock: Any, settings: Settings) -> Any:
    """Build the LLM mixin without importing ``SettingsService`` (cycle-free)."""
    from oft.application.settings_service import SettingsService

    return SettingsService(store=store, clock=clock, settings=settings)


__all__ = [
    "LLMSettings",
    "LLMSettingsMixin",
    "resolve_effective_llm_settings",
]
