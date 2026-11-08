"""Prompt lifecycle: list/show/edit/activate/restore, rendering, and provenance.

Versions are immutable and activation is explicit (ADR-0009). A draft is never
used by a scan; a missing historical version reports ``missing`` and disables
exact replay instead of silently substituting the current template.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from oft.application.kill_switch import record_audit
from oft.domain.prompts import (
    DEFAULT_SYSTEM_BODIES,
    DEFAULT_USER_BODIES,
    PROMPT_IDS,
    PROMPT_VERSION_MISSING,
    REQUIRED_VARIABLES,
    ROLE_BY_PROMPT_ID,
    SYSTEM_PROMPT_IDS,
    PromptError,
    PromptProvenance,
    PromptRole,
    PromptStatus,
    PromptValidationError,
    PromptVersionMissingError,
    content_hash,
    is_system_prompt_id,
    prompt_id_for_role,
    render,
    rendered_prompt_hash,
    role_for_prompt_id,
    validate_body,
)
from oft.domain.time import utc_timestamp
from oft.ports.clock import ClockPort
from oft.ports.store import PromptTemplateRecord, PromptVersionRecord, StorePort

_SECRET_FIELDS = (
    "api_token",
    "binance_api_key",
    "binance_api_secret",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "telegram_bot_token",
)


class PromptNotFoundError(PromptError):
    """The requested prompt template or version does not exist."""

    code = "NOT_FOUND"


@dataclass(frozen=True)
class ActivePrompt:
    """The explicitly active version and its pinned system prompt."""

    prompt_id: str
    version: int
    body: str
    body_hash: str
    status: str
    system_prompt_id: str
    system_version: int
    system_body: str
    system_body_hash: str
    required_variables: tuple[str, ...]
    knowledge_artifact_hash: str | None


@dataclass(frozen=True)
class RenderedPrompt:
    """One rendered system+user pair plus the hashes persisted on a decision."""

    prompt_id: str
    version: int
    body_hash: str
    system_prompt_id: str
    system_version: int
    system_prompt_hash: str
    system: str
    user: str
    prompt_hash: str
    knowledge_artifact_hash: str | None


@dataclass(frozen=True)
class PromptSummary:
    prompt_id: str
    role: str
    kind: str
    active_version: int
    version_count: int
    latest_version: int
    updated_at: str


@dataclass(frozen=True)
class PromptVersionView:
    prompt_id: str
    version: int
    status: str
    is_active: bool
    body: str
    body_hash: str
    required_variables: tuple[str, ...]
    author: str
    reason: str
    is_default: bool
    created_at: str
    system_prompt_id: str
    system_prompt_version: int
    system_prompt_hash: str
    knowledge_artifact_hash: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "status": self.status,
            "is_active": self.is_active,
            "body": self.body,
            "body_hash": self.body_hash,
            "required_variables": list(self.required_variables),
            "author": self.author,
            "reason": self.reason,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "system_prompt_id": self.system_prompt_id,
            "system_prompt_version": self.system_prompt_version,
            "system_prompt_hash": self.system_prompt_hash,
            "knowledge_artifact_hash": self.knowledge_artifact_hash,
        }


@dataclass(frozen=True)
class PromptDetail:
    prompt_id: str
    role: str
    kind: str
    active_version: int
    created_at: str
    updated_at: str
    versions: tuple[PromptVersionView, ...]

    def to_contract(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "role": self.role,
            "kind": self.kind,
            "active_version": self.active_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "versions": [version.to_contract() for version in self.versions],
        }


def secret_values_from_settings(settings: Any) -> tuple[str, ...]:
    """Collect the configured secret values that must never reach a prompt."""
    values: list[str] = []
    for name in _SECRET_FIELDS:
        secret = getattr(settings, name, None)
        if secret is None:
            continue
        getter = getattr(secret, "get_secret_value", None)
        value = getter() if callable(getter) else str(secret)
        if value.strip():
            values.append(value)
    return tuple(values)


def resolve_prompt_id(value: str) -> str:
    raw = value.strip()
    if raw in ROLE_BY_PROMPT_ID:
        return raw
    lowered = raw.lower()
    if lowered in ROLE_BY_PROMPT_ID:
        return lowered
    upper = raw.upper()
    if upper in PromptRole.__members__:
        return prompt_id_for_role(PromptRole(upper))
    raise PromptNotFoundError(f"unknown prompt {value!r}")


def _role_for(prompt_id: str) -> PromptRole:
    role = role_for_prompt_id(prompt_id)
    if role is None:
        raise PromptNotFoundError(f"unknown prompt {prompt_id!r}")
    return role


def _parse_required(raw: str, role: PromptRole) -> tuple[str, ...]:
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return REQUIRED_VARIABLES[role]
    if not isinstance(values, list):
        return REQUIRED_VARIABLES[role]
    return tuple(str(value) for value in values)


class PromptService:
    def __init__(self, *, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    # -- reads -----------------------------------------------------------

    def list_templates(self) -> tuple[PromptSummary, ...]:
        summaries: list[PromptSummary] = []
        for template in self._store.list_prompt_templates():
            versions = self._store.list_prompt_versions(template.prompt_id)
            summaries.append(
                PromptSummary(
                    prompt_id=template.prompt_id,
                    role=template.role,
                    kind="SYSTEM" if is_system_prompt_id(template.prompt_id) else "USER",
                    active_version=template.active_version,
                    version_count=len(versions),
                    latest_version=versions[-1].version if versions else 0,
                    updated_at=template.updated_at,
                )
            )
        return tuple(summaries)

    def show(self, prompt_id: str) -> PromptDetail:
        resolved = resolve_prompt_id(prompt_id)
        template = self._template(resolved)
        return PromptDetail(
            prompt_id=template.prompt_id,
            role=template.role,
            kind="SYSTEM" if is_system_prompt_id(template.prompt_id) else "USER",
            active_version=template.active_version,
            created_at=template.created_at,
            updated_at=template.updated_at,
            versions=tuple(
                self._view(record)
                for record in self._store.list_prompt_versions(template.prompt_id)
            ),
        )

    def history(self, prompt_id: str) -> tuple[PromptVersionView, ...]:
        return self.show(prompt_id).versions

    def diff(self, prompt_id: str, from_version: int, to_version: int) -> str:
        resolved = resolve_prompt_id(prompt_id)
        before = self._store.get_prompt_version(resolved, from_version)
        after = self._store.get_prompt_version(resolved, to_version)
        if before is None or after is None:
            missing = from_version if before is None else to_version
            raise PromptVersionMissingError(
                f"prompt version {missing} does not exist for {resolved}"
            )
        lines = difflib.unified_diff(
            before.body.splitlines(),
            after.body.splitlines(),
            fromfile=f"{resolved} v{from_version}",
            tofile=f"{resolved} v{to_version}",
            lineterm="",
        )
        return "\n".join(lines)

    # -- mutations -------------------------------------------------------

    def edit(
        self,
        prompt_id: str,
        body: str,
        *,
        author: str = "cli",
        reason: str = "",
        activate: bool = False,
        knowledge_artifact_hash: str | None = None,
    ) -> PromptVersionView:
        return self._create_version(
            prompt_id,
            body,
            author=author,
            reason=reason,
            activate=activate,
            is_default=False,
            knowledge_artifact_hash=knowledge_artifact_hash,
        )

    def activate(
        self, prompt_id: str, version: int, *, actor: str = "cli", reason: str = ""
    ) -> PromptVersionView:
        resolved = resolve_prompt_id(prompt_id)
        self._template(resolved)
        record = self._store.get_prompt_version(resolved, version)
        if record is None:
            raise PromptVersionMissingError(
                f"prompt version {version} does not exist for {resolved}"
            )
        role = _role_for(resolved)
        validate_body(record.body, role=role, system=is_system_prompt_id(resolved))
        now = utc_timestamp(self._clock.now())
        if not self._store.activate_prompt_version(resolved, version, activated_at=now):
            raise PromptVersionMissingError(
                f"prompt version {version} does not exist for {resolved}"
            )
        record_audit(
            self._store,
            event_type="PROMPT_VERSION_ACTIVATED",
            actor={"kind": "USER", "id": actor},
            subject={"kind": "PROMPT", "id": resolved},
            payload={"version": version, "reason": reason},
            at=now,
        )
        updated = self._store.get_prompt_version(resolved, version)
        assert updated is not None
        return self._view(updated)

    def restore_default(
        self, prompt_id: str, *, author: str = "cli", reason: str = "restore default"
    ) -> PromptVersionView:
        resolved = resolve_prompt_id(prompt_id)
        role = _role_for(resolved)
        body = (
            DEFAULT_SYSTEM_BODIES[role]
            if is_system_prompt_id(resolved)
            else DEFAULT_USER_BODIES[role]
        )
        return self._create_version(
            resolved,
            body,
            author=author,
            reason=reason,
            activate=False,
            is_default=True,
        )

    def _create_version(
        self,
        prompt_id: str,
        body: str,
        *,
        author: str,
        reason: str,
        activate: bool,
        is_default: bool,
        knowledge_artifact_hash: str | None = None,
    ) -> PromptVersionView:
        resolved = resolve_prompt_id(prompt_id)
        self._template(resolved)
        role = _role_for(resolved)
        system = is_system_prompt_id(resolved)
        validate_body(body, role=role, system=system)

        versions = self._store.list_prompt_versions(resolved)
        next_version = (versions[-1].version + 1) if versions else 1
        if system:
            system_prompt_id = resolved
            system_version = next_version
            system_body = body
            system_body_hash = content_hash(body)
            required: tuple[str, ...] = ()
        else:
            system_prompt_id, system_version, system_body, system_body_hash = (
                self._active_system_pin(role)
            )
            required = REQUIRED_VARIABLES[role]

        now = utc_timestamp(self._clock.now())
        record = PromptVersionRecord(
            prompt_id=resolved,
            version=next_version,
            body=body,
            body_hash=content_hash(body),
            is_active=False,
            created_at=now,
            system_prompt_id=system_prompt_id,
            system_prompt_version=system_version,
            system_body=system_body,
            system_body_hash=system_body_hash,
            required_variables_json=json.dumps(list(required)),
            knowledge_artifact_hash=knowledge_artifact_hash,
            status=PromptStatus.DRAFT.value,
            author=author,
            reason=reason,
            is_default=is_default,
        )
        self._store.save_prompt_version(record)
        record_audit(
            self._store,
            event_type=("PROMPT_DEFAULT_RESTORED" if is_default else "PROMPT_VERSION_CREATED"),
            actor={"kind": "USER", "id": author},
            subject={"kind": "PROMPT", "id": resolved},
            payload={
                "version": next_version,
                "body_hash": record.body_hash,
                "reason": reason,
            },
            at=now,
        )
        if activate:
            return self.activate(resolved, next_version, actor=author, reason=reason)
        return self._view(record)

    # -- rendering -------------------------------------------------------

    def active_for(self, role: str | PromptRole) -> ActivePrompt:
        resolved_role = role if isinstance(role, PromptRole) else PromptRole(str(role))
        prompt_id = PROMPT_IDS[resolved_role]
        template = self._store.get_prompt_template(prompt_id)
        if template is None:
            return self._builtin_active(resolved_role)
        record = self._store.get_prompt_version(prompt_id, template.active_version)
        if record is None:
            raise PromptVersionMissingError(
                f"active prompt version {template.active_version} is missing for {prompt_id}"
            )
        if record.status != PromptStatus.ACTIVE.value or not record.is_active:
            raise PromptVersionMissingError(
                f"prompt version {record.version} for {prompt_id} is not active"
            )
        system_body = record.system_body or DEFAULT_SYSTEM_BODIES[resolved_role]
        system_hash = record.system_body_hash or content_hash(system_body)
        return ActivePrompt(
            prompt_id=prompt_id,
            version=record.version,
            body=record.body,
            body_hash=record.body_hash,
            status=record.status,
            system_prompt_id=record.system_prompt_id or SYSTEM_PROMPT_IDS[resolved_role],
            system_version=record.system_prompt_version,
            system_body=system_body,
            system_body_hash=system_hash,
            required_variables=_parse_required(record.required_variables_json, resolved_role),
            knowledge_artifact_hash=record.knowledge_artifact_hash,
        )

    def render_active(
        self,
        role: str | PromptRole,
        values: Mapping[str, object],
        *,
        secret_values: Sequence[str] = (),
    ) -> RenderedPrompt:
        resolved_role = role if isinstance(role, PromptRole) else PromptRole(str(role))
        active = self.active_for(resolved_role)
        system = render(active.system_body, {}, secret_values=secret_values)
        user = render(active.body, values, role=resolved_role, secret_values=secret_values)
        return RenderedPrompt(
            prompt_id=active.prompt_id,
            version=active.version,
            body_hash=active.body_hash,
            system_prompt_id=active.system_prompt_id,
            system_version=active.system_version,
            system_prompt_hash=content_hash(system),
            system=system,
            user=user,
            prompt_hash=rendered_prompt_hash(active.prompt_id, active.version, system, user),
            knowledge_artifact_hash=active.knowledge_artifact_hash,
        )

    # -- provenance ------------------------------------------------------

    def resolve_decision(self, decision: Mapping[str, Any]) -> PromptProvenance:
        provenance = decision.get("provenance")
        if not isinstance(provenance, Mapping):
            return PromptProvenance(
                status="missing",
                prompt_id="",
                version=0,
                exact_replay=False,
                code=PROMPT_VERSION_MISSING,
            )
        prompt_id = provenance.get("prompt_id")
        version = provenance.get("prompt_version")
        if not isinstance(prompt_id, str) or not isinstance(version, int) or version < 1:
            return PromptProvenance(
                status="missing",
                prompt_id=prompt_id if isinstance(prompt_id, str) else "",
                version=version if isinstance(version, int) else 0,
                exact_replay=False,
                code=PROMPT_VERSION_MISSING,
            )
        record = self._store.get_prompt_version(prompt_id, version)
        if record is None:
            return PromptProvenance(
                status="missing",
                prompt_id=prompt_id,
                version=version,
                exact_replay=False,
                code=PROMPT_VERSION_MISSING,
            )
        return PromptProvenance(
            status="available",
            prompt_id=prompt_id,
            version=version,
            exact_replay=True,
            code=None,
            prompt_hash=record.body_hash,
        )

    # -- helpers ---------------------------------------------------------

    def _template(self, prompt_id: str) -> PromptTemplateRecord:
        template = self._store.get_prompt_template(prompt_id)
        if template is None:
            raise PromptNotFoundError(f"unknown prompt {prompt_id!r}")
        return template

    def _view(self, record: PromptVersionRecord) -> PromptVersionView:
        role = _role_for(record.prompt_id)
        system_hash = record.system_body_hash or content_hash(record.system_body)
        return PromptVersionView(
            prompt_id=record.prompt_id,
            version=record.version,
            status=record.status,
            is_active=record.is_active,
            body=record.body,
            body_hash=record.body_hash,
            required_variables=_parse_required(record.required_variables_json, role),
            author=record.author,
            reason=record.reason,
            is_default=record.is_default,
            created_at=record.created_at,
            system_prompt_id=record.system_prompt_id,
            system_prompt_version=record.system_prompt_version,
            system_prompt_hash=system_hash,
            knowledge_artifact_hash=record.knowledge_artifact_hash,
        )

    def _active_system_pin(self, role: PromptRole) -> tuple[str, int, str, str]:
        system_id = SYSTEM_PROMPT_IDS[role]
        template = self._store.get_prompt_template(system_id)
        if template is not None:
            record = self._store.get_prompt_version(system_id, template.active_version)
            if record is not None:
                return (
                    system_id,
                    record.version,
                    record.body,
                    record.body_hash or content_hash(record.body),
                )
        body = DEFAULT_SYSTEM_BODIES[role]
        return system_id, 1, body, content_hash(body)

    def _builtin_active(self, role: PromptRole) -> ActivePrompt:
        body = DEFAULT_USER_BODIES[role]
        system_body = DEFAULT_SYSTEM_BODIES[role]
        return ActivePrompt(
            prompt_id=PROMPT_IDS[role],
            version=1,
            body=body,
            body_hash=content_hash(body),
            status=PromptStatus.ACTIVE.value,
            system_prompt_id=SYSTEM_PROMPT_IDS[role],
            system_version=1,
            system_body=system_body,
            system_body_hash=content_hash(system_body),
            required_variables=REQUIRED_VARIABLES[role],
            knowledge_artifact_hash=None,
        )


__all__ = [
    "ActivePrompt",
    "PromptDetail",
    "PromptNotFoundError",
    "PromptService",
    "PromptSummary",
    "PromptValidationError",
    "PromptVersionMissingError",
    "PromptVersionView",
    "RenderedPrompt",
    "resolve_prompt_id",
    "secret_values_from_settings",
]
