"""``oft prompt list|show|edit|activate|restore|diff|history`` (ADR-0009)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.prompt_service import (
    PromptDetail,
    PromptNotFoundError,
    PromptService,
    PromptVersionView,
    resolve_prompt_id,
)
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings
from oft.domain.prompts import PromptError

prompt_app = typer.Typer(
    help="Prompt template versions: list, show, edit, activate, restore, diff, history.",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _service(settings: Any) -> PromptService:
    container = build_container(settings)
    return PromptService(store=container.store, clock=container.clock)


def _render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No prompt templates. Start the daemon to seed the built-in defaults."
    width = max(len(str(item["prompt_id"])) for item in items)
    lines = [f"{'PROMPT':<{width}}  {'ROLE':<14}{'KIND':<8}{'ACTIVE':<8}VERSIONS"]
    for item in items:
        lines.append(
            f"{str(item['prompt_id']):<{width}}  {str(item['role']):<14}"
            f"{str(item['kind']):<8}v{str(item['active_version']):<7}"
            f"{item['version_count']}"
        )
    return "\n".join(lines)


def _render_detail(detail: PromptDetail) -> str:
    versions = detail.versions
    latest = versions[-1].version if versions else 0
    system = versions[-1] if versions else None
    lines = [
        f"Prompt            {detail.prompt_id} ({detail.kind})",
        f"Role              {detail.role}",
        f"Active version    v{detail.active_version}",
        f"Latest version    v{latest}",
    ]
    if system is not None:
        lines.append(
            f"System prompt     {system.system_prompt_id} v{system.system_prompt_version}"
            f" (sha256 {system.system_prompt_hash[:12]})"
        )
        lines.append(
            "Required vars     " + ", ".join(system.required_variables)
            if system.required_variables
            else "Required vars     (none; system prose)"
        )
        lines.append("")
        lines.append("Body")
        lines.append(system.body.rstrip())
    lines.append("")
    lines.append("Versions")
    for version in versions:
        marker = "default" if version.is_default else version.reason or "-"
        lines.append(f"  v{version.version:<4}{version.status:<12}{marker}")
    return "\n".join(lines)


def _render_history(prompt_id: str, versions: tuple[PromptVersionView, ...]) -> str:
    lines = [f"HISTORY · {prompt_id}"]
    for version in versions:
        marker = "default" if version.is_default else version.reason or "-"
        lines.append(f"v{version.version:<4}{version.status:<12}{version.body_hash[:12]}  {marker}")
    return "\n".join(lines)


def _emit_json(data: dict[str, Any], *, data_quality: str = "COMPLETE") -> None:
    typer.echo(
        json.dumps(
            success_envelope(
                data,
                request_id=new_request_id(),
                provenance="CALC",
                data_quality=data_quality,
            ),
            indent=2,
        )
    )


def _local_service_or_exit(json_output: bool) -> PromptService:
    return _service(_settings_or_exit(json_output))


def _handle_prompt_error(exc: PromptError, json_output: bool) -> None:
    details = (
        [{"field": exc.variable, "code": exc.code, "message": exc.message}]
        if exc.variable is not None
        else None
    )
    emit_error(exc.code, exc.message, json_output=json_output, details=details)


@prompt_app.command("list")
def list_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List prompt families, roles, active version, and version counts."""
    service = _local_service_or_exit(json_output)
    items = [asdict(summary) for summary in service.list_templates()]
    if json_output:
        _emit_json(
            {"items": items, "total": len(items)},
            data_quality="COMPLETE" if items else "MISSING",
        )
        return
    typer.echo(_render_list(items))


@prompt_app.command("show")
def show_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role (analysis/ANALYSIS).")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one prompt with its body and immutable version history."""
    service = _local_service_or_exit(json_output)
    try:
        detail = service.show(prompt)
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        _emit_json(detail.to_contract())
        return
    typer.echo(_render_detail(detail))


@prompt_app.command("history")
def history_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List every version of one prompt."""
    service = _local_service_or_exit(json_output)
    try:
        resolved = resolve_prompt_id(prompt)
        versions = service.history(resolved)
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        _emit_json(
            {
                "prompt_id": resolved,
                "items": [version.to_contract() for version in versions],
                "total": len(versions),
            }
        )
        return
    typer.echo(_render_history(resolved, versions))


@prompt_app.command("diff")
def diff_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role.")],
    from_version: Annotated[int, typer.Option("--from", help="Base version.")],
    to_version: Annotated[int, typer.Option("--to", help="Target version.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show a unified diff between two immutable versions."""
    service = _local_service_or_exit(json_output)
    try:
        resolved = resolve_prompt_id(prompt)
        diff = service.diff(resolved, from_version, to_version)
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        _emit_json(
            {
                "prompt_id": resolved,
                "from_version": from_version,
                "to_version": to_version,
                "diff": diff,
            }
        )
        return
    typer.echo(diff)


def _api_or_exit(settings: Any, json_output: bool) -> ApiClient:
    return ApiClient.from_settings(settings)


def _body_from_options(body: str | None, file: Path | None, json_output: bool) -> str:
    if body is not None and file is not None:
        emit_error(
            "VALIDATION_FAILED", "Use either --body or --file, not both.", json_output=json_output
        )
        raise typer.Exit(code=2)
    if file is not None:
        try:
            return file.read_text(encoding="utf-8")
        except OSError as exc:
            emit_error("VALIDATION_FAILED", f"cannot read {file}: {exc}", json_output=json_output)
            raise typer.Exit(code=2) from exc
    if body is None:
        emit_error(
            "VALIDATION_FAILED",
            "Provide --body or --file with the template.",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    return body


@prompt_app.command("edit")
def edit_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role.")],
    body: Annotated[str | None, typer.Option("--body", help="New template body.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="Read the body from a file.")] = None,
    author: Annotated[
        str, typer.Option("--author", help="Author recorded on the version.")
    ] = "cli",
    reason: Annotated[str, typer.Option("--reason", help="Why this version exists.")] = "",
    activate: Annotated[
        bool, typer.Option("--activate", help="Activate the new version immediately.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Create a new immutable version; the old versions stay readable."""
    text = _body_from_options(body, file, json_output)
    settings = _settings_or_exit(json_output)
    try:
        envelope = _api_or_exit(settings, json_output).edit_prompt(
            resolve_prompt_id(prompt),
            {"body": text, "author": author, "reason": reason, "activate": activate},
            idempotency_key=f"cli-{uuid.uuid4().hex}",
        )
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    typer.echo(f"Created {data.get('prompt_id')} v{data.get('version')} ({data.get('status')})")


@prompt_app.command("activate")
def activate_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role.")],
    version: Annotated[int, typer.Option("--version", help="Version to activate.")],
    actor: Annotated[
        str, typer.Option("--actor", help="Actor recorded in the audit chain.")
    ] = "cli",
    reason: Annotated[str, typer.Option("--reason", help="Why the version is activated.")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Explicitly switch the active version (a draft is never used implicitly)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = _api_or_exit(settings, json_output).activate_prompt(
            resolve_prompt_id(prompt),
            {"version": version, "actor": actor, "reason": reason},
            idempotency_key=f"cli-{uuid.uuid4().hex}",
        )
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    typer.echo(f"Activated {data.get('prompt_id')} v{data.get('version')}")


@prompt_app.command("restore")
def restore_command(
    prompt: Annotated[str, typer.Argument(help="Prompt id or role.")],
    author: Annotated[
        str, typer.Option("--author", help="Author recorded on the version.")
    ] = "cli",
    reason: Annotated[
        str, typer.Option("--reason", help="Why the default is restored.")
    ] = "restore default",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Create a new version with the built-in default body (history is kept)."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = _api_or_exit(settings, json_output).restore_prompt(
            resolve_prompt_id(prompt),
            {"author": author, "reason": reason},
            idempotency_key=f"cli-{uuid.uuid4().hex}",
        )
    except PromptError as exc:
        _handle_prompt_error(exc, json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    typer.echo(
        f"Restored {data.get('prompt_id')} v{data.get('version')} ({data.get('status')}, default)"
    )


__all__ = [
    "PromptNotFoundError",
    "prompt_app",
]
