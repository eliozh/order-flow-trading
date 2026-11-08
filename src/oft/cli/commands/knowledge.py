"""``oft knowledge build|review|activate|status`` (ADR-0009)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.knowledge_service import KnowledgeService
from oft.application.prompt_builder import sanitize_knowledge_text
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, Settings, load_settings
from oft.domain.knowledge import (
    KNOWLEDGE_ABSENT,
    KNOWLEDGE_ARTIFACT_MISMATCH,
    KNOWLEDGE_LOCATION,
    KNOWLEDGE_MANIFEST_INVALID,
    KNOWLEDGE_NO_SECTIONS,
    KNOWLEDGE_NOT_ACTIVE,
    KNOWLEDGE_NOT_REVIEWED,
    KNOWLEDGE_SOURCE_EMPTY,
    KNOWLEDGE_SOURCE_TOO_LARGE,
    PRIVATE_WARNING,
    KnowledgeError,
)

knowledge_app = typer.Typer(
    help="Private order-flow playbook: build, review, activate, status.",
    no_args_is_help=True,
    add_completion=False,
)

_ERROR_CODES = {
    KNOWLEDGE_SOURCE_EMPTY: "VALIDATION_FAILED",
    KNOWLEDGE_SOURCE_TOO_LARGE: "VALIDATION_FAILED",
    KNOWLEDGE_NO_SECTIONS: "VALIDATION_FAILED",
    KNOWLEDGE_MANIFEST_INVALID: "VALIDATION_FAILED",
    KNOWLEDGE_LOCATION: "CONFLICT",
    KNOWLEDGE_ARTIFACT_MISMATCH: "CONFLICT",
    KNOWLEDGE_NOT_REVIEWED: "CONFLICT",
    KNOWLEDGE_NOT_ACTIVE: "CONFLICT",
    KNOWLEDGE_ABSENT: "NOT_FOUND",
}


def _settings_or_exit(json_output: bool) -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _service(settings: Settings, *, initialize: bool = False) -> KnowledgeService:
    container = build_container(settings, initialize=initialize)
    return KnowledgeService(
        settings=settings,
        clock=container.clock,
        store=container.store,
    )


def _sanitize_config(value: Any) -> Any:
    """Apply the knowledge sanitizer to every string in a config tree."""
    if isinstance(value, str):
        return sanitize_knowledge_text(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_config(item) for item in value]
    return value


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


def _fail(exc: KnowledgeError, json_output: bool) -> None:
    code = _ERROR_CODES.get(exc.code, "VALIDATION_FAILED")
    emit_error(code, exc.message, json_output=json_output)
    raise typer.Exit(code=exit_code_for(code))


def _warn_private() -> None:
    typer.echo(f"warning: {PRIVATE_WARNING}")


@knowledge_app.command("build")
def build_command(
    source: Annotated[
        Path,
        typer.Option("--source", help="Skill markdown file or directory."),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help=(
                "Artifact directory; persisted so review/activate/status find it "
                "(default: $OFT_DATA/knowledge/playbook)."
            ),
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compile a private playbook, topic index, rules, and manifest."""
    settings = _settings_or_exit(json_output)
    try:
        result = _service(settings).build(source, output=output)
    except KnowledgeError as exc:
        _fail(exc, json_output)
        return
    manifest = result.artifact.manifest
    data = {
        "artifact_id": manifest.artifact_id,
        "compiler_version": manifest.compiler_version,
        "token_count": manifest.token_count,
        "token_count_method": manifest.token_count_method,
        "playbook_token_budget": manifest.playbook_token_budget,
        "section_ids": list(manifest.sections),
        "topics": list(manifest.topics),
        "dropped": [dict(item) for item in manifest.dropped],
        "summarized": manifest.summarized,
        "created_at": manifest.created_at,
        "review_status": manifest.review_status,
        "reviewed_by": manifest.reviewed_by,
        "license_note": manifest.license_note,
        "private": True,
        "warning": PRIVATE_WARNING,
        "files": list(result.files),
        "output_dir": str(result.output_dir),
    }
    if json_output:
        _emit_json(data)
        return
    _warn_private()
    typer.echo(f"Built knowledge artifact {manifest.artifact_id[:12]}…")
    typer.echo(
        f"  sections {len(manifest.sections)} · tokens {manifest.token_count}"
        f" ({manifest.token_count_method}) · dropped {len(manifest.dropped)}"
    )
    typer.echo(f"  output   {result.output_dir}")
    typer.echo("  next     oft knowledge review --reviewed-by <name>")
    typer.echo("           oft knowledge activate")


@knowledge_app.command("review")
def review_command(
    reviewed_by: Annotated[
        str, typer.Option("--reviewed-by", help="Named human reviewer (required).")
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Record human sign-off on the compiled playbook."""
    settings = _settings_or_exit(json_output)
    service = _service(settings, initialize=True)
    try:
        artifact = service.load_artifact()
        state = service.review(reviewed_by=reviewed_by)
    except KnowledgeError as exc:
        _fail(exc, json_output)
        return
    # Show exactly what is being approved: a hostile skill pack's rules are not
    # injected as ROLE RULES unseen, and rule text is sanitized as data.
    safe_playbook = sanitize_knowledge_text(artifact.playbook)
    safe_rules = _sanitize_config(artifact.rules)
    if json_output:
        data = state.to_dict()
        data["warning"] = PRIVATE_WARNING
        data["playbook"] = safe_playbook
        data["rules"] = safe_rules
        _emit_json(data)
        return
    _warn_private()
    typer.echo(f"Reviewed knowledge artifact {str(state.artifact_id)[:12]}… by {reviewed_by}")
    typer.echo("compiled playbook under review:")
    typer.echo(safe_playbook)
    typer.echo("compiled role rules under review:")
    typer.echo(json.dumps(safe_rules, indent=2, sort_keys=True))
    typer.echo("  next     oft knowledge activate")


@knowledge_app.command("activate")
def activate_command(
    actor: Annotated[
        str, typer.Option("--actor", help="Actor recorded in the audit chain.")
    ] = "cli",
    reason: Annotated[str, typer.Option("--reason", help="Why this artifact is activated.")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Activate a reviewed artifact so scans inject it deterministically."""
    settings = _settings_or_exit(json_output)
    try:
        state = _service(settings, initialize=True).activate(actor=actor, reason=reason)
    except KnowledgeError as exc:
        _fail(exc, json_output)
        return
    if json_output:
        data = state.to_dict()
        data["warning"] = PRIVATE_WARNING
        _emit_json(data)
        return
    _warn_private()
    typer.echo(f"Activated knowledge artifact {str(state.artifact_id)[:12]}…")


@knowledge_app.command("status")
def status_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the compiled/active knowledge state (explicit when absent)."""
    settings = _settings_or_exit(json_output)
    state = _service(settings).load_state()
    data = state.to_dict()
    data["warning"] = PRIVATE_WARNING
    if json_output:
        _emit_json(
            data,
            data_quality="COMPLETE" if state.status == "AVAILABLE" else "MISSING",
        )
        return
    typer.echo(f"knowledge status · {state.status}")
    typer.echo(f"  artifact    {state.artifact_id or '(none)'}")
    typer.echo(f"  active      {'yes' if state.active else 'no'}")
    typer.echo(f"  reviewed_by {state.reviewed_by or '(unreviewed)'}")
    typer.echo(f"  tokens      {state.token_count if state.token_count is not None else '-'}")
    if state.reason:
        typer.echo(f"  reason      {state.reason}")
    typer.echo(f"  replay      {'exact' if state.exact_replay else 'exact replay disabled'}")
    _warn_private()


__all__ = ["knowledge_app"]
