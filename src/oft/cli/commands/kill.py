"""``oft kill status|on|off``: the immediate kill switch.

Activation never prompts. Deactivation requires the typed ``RESUME`` token; a
non-interactive invocation must pass ``--token RESUME`` explicitly.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

kill_app = typer.Typer(
    help="Kill switch: block new paper orders immediately.",
    no_args_is_help=True,
    add_completion=False,
)

RESUME_TOKEN = "RESUME"


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit_status(settings: Any, json_output: bool) -> None:
    try:
        envelope = ApiClient.from_settings(settings).kill_status()
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    typer.echo(f"Kill switch {'ACTIVE' if data.get('active') else 'OFF'}")
    typer.echo(f"sentinel {data.get('sentinel_path', '-')}")


@kill_app.command("status")
def kill_status(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the kill-switch state (DB + file sentinel)."""
    _emit_status(_settings_or_exit(json_output), json_output)


@kill_app.command("on")
def kill_on(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Activate immediately; no confirmation is required."""
    settings = _settings_or_exit(json_output)
    try:
        envelope = ApiClient.from_settings(settings).set_kill_switch(active=True)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo("Kill switch ACTIVE. New paper orders and retries are blocked.")


@kill_app.command("off")
def kill_off(
    token: Annotated[
        str | None,
        typer.Option("--token", help="Typed RESUME confirmation (required non-interactively)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Deactivate; requires the typed RESUME token and never auto-closes positions."""
    settings = _settings_or_exit(json_output)
    provided = token
    if provided is None:
        if sys.stdin.isatty():
            provided = typer.prompt("Type RESUME to deactivate the kill switch")
        else:
            emit_error(
                "VALIDATION_FAILED",
                "deactivation requires --token RESUME in non-interactive mode",
                json_output=json_output,
            )
            raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    if provided.strip() != RESUME_TOKEN:
        emit_error(
            "VALIDATION_FAILED",
            "the typed confirmation must be exactly RESUME",
            json_output=json_output,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED"))
    try:
        envelope = ApiClient.from_settings(settings).set_kill_switch(
            active=False, token=RESUME_TOKEN
        )
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo("Kill switch OFF. Open positions remain and are visible.")
