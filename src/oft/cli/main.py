"""The ``oft`` command tree."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.application.status import StatusReport
from oft.bootstrap import get_status
from oft.cli.output import render_status_table
from oft.config import load_settings

app = typer.Typer(
    help="Order Flow Trading local CLI.",
    no_args_is_help=True,
    add_completion=False,
)


def _status_envelope(report: StatusReport) -> dict[str, Any]:
    return success_envelope(
        report.model_dump(mode="json"),
        request_id=new_request_id(),
        generated_at=report.generated_at,
        provenance="CALC",
        data_quality="COMPLETE",
    )


@app.callback()
def _root() -> None:
    """Order Flow Trading local CLI."""


@app.command()
def status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Show daemon-independent system status."""
    report = get_status(load_settings())
    if json_output:
        typer.echo(json.dumps(_status_envelope(report), indent=2))
    else:
        typer.echo(render_status_table(report))


def main() -> None:
    app()
