"""``oft status``: daemon-independent system status."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.health import probe_daemon
from oft.application.status import build_status
from oft.bootstrap import build_container
from oft.cli.output import render_status_table
from oft.config import ConfigError, load_settings


def _emit_config_error(config_error: ConfigError, json_output: bool) -> None:
    from oft.cli.commands.doctor import emit_error

    emit_error("CONFIG_INVALID", config_error.message, json_output=json_output)
    raise typer.Exit(code=5)


def status_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Show daemon-independent system status."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        _emit_config_error(exc, json_output)
        return

    container = build_container(settings)
    report = build_status(
        container.settings,
        container.market_data,
        container.clock,
        store=container.store,
        daemon=probe_daemon(settings),
    )
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    report.model_dump(mode="json"),
                    request_id=new_request_id(),
                    generated_at=report.generated_at,
                    provenance="CALC",
                    data_quality="COMPLETE",
                ),
                indent=2,
            )
        )
    else:
        typer.echo(render_status_table(report))
