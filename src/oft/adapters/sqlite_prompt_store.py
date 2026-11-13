"""Prompt-template and provider-capability mixin.

Covers the ``prompt_templates``, ``prompt_versions``, and
``provider_capabilities`` tables plus their row mappers.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from oft.adapters.sqlite_audit import SqliteAuditMixin
from oft.adapters.sqlite_connections import SqliteConnectionMixin
from oft.domain.prompts import PromptRole, PromptStatus
from oft.ports.store import (
    PromptTemplateRecord,
    PromptVersionRecord,
    ProviderCapabilityRecord,
)


class PromptStoreMixin(SqliteConnectionMixin, SqliteAuditMixin):
    """Read and write prompt templates, immutable versions, and provider probes."""

    def save_prompt_template(self, record: PromptTemplateRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO prompt_templates
                    (prompt_id, role, active_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(prompt_id) DO UPDATE SET
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (
                    record.prompt_id,
                    record.role,
                    record.active_version,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def get_prompt_template(self, prompt_id: str) -> PromptTemplateRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM prompt_templates WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_prompt_template(row)

    def list_prompt_templates(self) -> tuple[PromptTemplateRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM prompt_templates ORDER BY prompt_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_prompt_template(row) for row in rows)

    def save_prompt_version(self, record: PromptVersionRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO prompt_versions (
                    prompt_id, version, body, body_hash, is_active, created_at,
                    system_prompt_id, system_prompt_version, system_body,
                    system_body_hash, required_variables_json, knowledge_artifact_hash,
                    status, author, reason, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.prompt_id,
                    record.version,
                    record.body,
                    record.body_hash,
                    int(record.is_active),
                    record.created_at,
                    record.system_prompt_id,
                    record.system_prompt_version,
                    record.system_body,
                    record.system_body_hash,
                    record.required_variables_json,
                    record.knowledge_artifact_hash,
                    record.status,
                    record.author,
                    record.reason,
                    int(record.is_default),
                ),
            )

    def get_prompt_version(self, prompt_id: str, version: int) -> PromptVersionRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_prompt_version(row)

    def list_prompt_versions(self, prompt_id: str) -> tuple[PromptVersionRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version",
                (prompt_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_prompt_version(row) for row in rows)

    def activate_prompt_version(self, prompt_id: str, version: int, *, activated_at: str) -> bool:
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM prompt_versions WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                """
                UPDATE prompt_versions
                SET is_active = 0, status = ?
                WHERE prompt_id = ? AND version <> ? AND is_active = 1
                """,
                (
                    PromptStatus.SUPERSEDED.value,
                    prompt_id,
                    version,
                ),
            )
            connection.execute(
                "UPDATE prompt_versions SET is_active = 1, status = ? "
                "WHERE prompt_id = ? AND version = ?",
                (PromptStatus.ACTIVE.value, prompt_id, version),
            )
            connection.execute(
                "UPDATE prompt_templates SET active_version = ?, updated_at = ? "
                "WHERE prompt_id = ?",
                (version, activated_at, prompt_id),
            )
            return True

    def save_provider_capability(self, record: ProviderCapabilityRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO provider_capabilities (
                    provider, model, strict_schema, degraded, max_output_tokens,
                    status, error_code, notes, latency_ms, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    model = excluded.model,
                    strict_schema = excluded.strict_schema,
                    degraded = excluded.degraded,
                    max_output_tokens = excluded.max_output_tokens,
                    status = excluded.status,
                    error_code = excluded.error_code,
                    notes = excluded.notes,
                    latency_ms = excluded.latency_ms,
                    checked_at = excluded.checked_at
                """,
                (
                    record.provider,
                    record.model,
                    int(record.strict_schema),
                    int(record.degraded),
                    record.max_output_tokens,
                    record.status,
                    record.error_code,
                    record.notes,
                    record.latency_ms,
                    record.checked_at,
                ),
            )

    def get_provider_capability(self, provider: str) -> ProviderCapabilityRecord | None:
        connection = self._connect_read_only()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM provider_capabilities WHERE provider = ?", (provider,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _row_to_provider_capability(row)

    def list_provider_capabilities(self) -> tuple[ProviderCapabilityRecord, ...]:
        connection = self._connect_read_only()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT * FROM provider_capabilities ORDER BY provider"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_provider_capability(row) for row in rows)


def _row_to_prompt_template(row: sqlite3.Row) -> PromptTemplateRecord:
    return PromptTemplateRecord(
        prompt_id=row["prompt_id"],
        role=PromptRole(row["role"]),
        active_version=row["active_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_prompt_version(row: sqlite3.Row) -> PromptVersionRecord:
    keys = row.keys()
    return PromptVersionRecord(
        prompt_id=row["prompt_id"],
        version=row["version"],
        body=row["body"],
        body_hash=row["body_hash"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        system_prompt_id=row["system_prompt_id"] if "system_prompt_id" in keys else "",
        system_prompt_version=(
            row["system_prompt_version"] if "system_prompt_version" in keys else 1
        ),
        system_body=row["system_body"] if "system_body" in keys else "",
        system_body_hash=row["system_body_hash"] if "system_body_hash" in keys else "",
        required_variables_json=(
            row["required_variables_json"] if "required_variables_json" in keys else "[]"
        ),
        knowledge_artifact_hash=(
            row["knowledge_artifact_hash"] if "knowledge_artifact_hash" in keys else None
        ),
        status=(PromptStatus(row["status"]) if "status" in keys else PromptStatus.ACTIVE),
        author=row["author"] if "author" in keys else "",
        reason=row["reason"] if "reason" in keys else "",
        is_default=bool(row["is_default"]) if "is_default" in keys else False,
    )


def _row_to_provider_capability(row: sqlite3.Row) -> ProviderCapabilityRecord:
    return ProviderCapabilityRecord(
        provider=row["provider"],
        model=row["model"],
        strict_schema=bool(row["strict_schema"]),
        degraded=bool(row["degraded"]),
        max_output_tokens=row["max_output_tokens"],
        status=row["status"],
        error_code=row["error_code"],
        notes=row["notes"],
        latency_ms=row["latency_ms"],
        checked_at=row["checked_at"],
    )
