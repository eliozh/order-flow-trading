"""``oft config show|get|set|validate``: the CLI half of ``scan.period``.

The values and exit codes match the settings API: an invalid value is
``VALIDATION_FAILED`` (exit 1) and writes nothing, a config-load failure is
``CONFIG_INVALID`` (exit 5), and a period change needs no confirmation.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.settings_service import SettingsService
from oft.bootstrap import build_container
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings
from oft.domain.settings import SCAN_PERIOD_KEY, SettingValidationError

config_app = typer.Typer(
    help="Runtime settings: show, get, set, or validate the persisted scan period.",
    no_args_is_help=True,
    add_completion=False,
)


def _load(json_output: bool) -> Any:
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc
    return build_container(settings, initialize=True)


def _service(container: Any) -> SettingsService:
    return SettingsService(store=container.store, clock=container.clock)


def _emit(json_output: bool, data: dict[str, Any]) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    data,
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )


def _require_scan_period(key: str, *, json_output: bool) -> str:
    if key != SCAN_PERIOD_KEY:
        emit_error("NOT_FOUND", f"unknown setting key {key!r}", json_output=json_output)
        raise typer.Exit(code=exit_code_for("NOT_FOUND"))
    return key


@config_app.command("show")
def config_show(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the effective scan period, its source, and the next UTC boundary."""
    container = _load(json_output)
    try:
        scan = _service(container).read_scan_settings()
    except ConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    data = scan.to_contract()
    if json_output:
        _emit(json_output, data)
        return
    typer.echo(f"scan.period      {scan.value}")
    typer.echo(f"Source           {scan.source.value}")
    typer.echo(f"Supported        {', '.join(scan.supported)}")
    typer.echo(f"Next run (UTC)   {scan.next_run}")
    if scan.updated_at is None:
        typer.echo("Last changed     never (default)")
    else:
        typer.echo(f"Last changed     {scan.updated_at} by {scan.updated_by}")


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument(help="Setting key; only scan.period exists.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print one effective setting value."""
    _require_scan_period(key, json_output=json_output)
    container = _load(json_output)
    try:
        scan = _service(container).read_scan_settings()
    except ConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    if json_output:
        _emit(
            json_output,
            {"key": scan.key, "value": scan.value, "source": scan.source.value},
        )
        return
    typer.echo(scan.value)


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting key; only scan.period exists.")],
    value: Annotated[str, typer.Argument(help="New value; one of 4h or 8h.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Validate and persist one setting; invalid values write nothing."""
    _require_scan_period(key, json_output=json_output)
    container = _load(json_output)
    service = _service(container)
    try:
        before = service.read_scan_settings()
        after = service.update_scan_period(value, actor="cli")
    except SettingValidationError as exc:
        emit_error(
            "VALIDATION_FAILED",
            exc.message,
            json_output=json_output,
            details=exc.details,
        )
        raise typer.Exit(code=exit_code_for("VALIDATION_FAILED")) from exc
    except ConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    if json_output:
        _emit(json_output, after.to_contract())
        return
    if before.value == after.value:
        typer.echo(f"{key} already {after.value} (no change, no audit event)")
    else:
        typer.echo(
            f"{key} {before.value} -> {after.value} (source: {after.source.value}; audit written)"
        )


@config_app.command("validate")
def config_validate(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Check the effective setting; never writes a config row."""
    container = _load(json_output)
    try:
        result = _service(container).validate_settings()
    except ConfigError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    if json_output:
        _emit(json_output, result)
        return
    typer.echo(f"PASS · {SCAN_PERIOD_KEY} {result['value']} · source {result['source']}")


__all__ = ["config_app", "config_get", "config_set", "config_show", "config_validate"]
