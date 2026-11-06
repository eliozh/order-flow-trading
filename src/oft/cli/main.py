"""The ``oft`` command tree."""

from __future__ import annotations

from typing import Annotated

import typer

from oft.cli.commands.doctor import doctor_command, emit_error
from oft.cli.commands.scan import scan_app
from oft.cli.commands.status import status_command
from oft.config import ConfigError, load_settings

app = typer.Typer(
    help="Order Flow Trading local CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Order Flow Trading local CLI."""


app.command(name="status")(status_command)
app.command(name="doctor")(doctor_command)
app.add_typer(scan_app, name="scan")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@app.command()
def serve(
    host: Annotated[str | None, typer.Option("--host", help="Loopback bind address.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="API port.")] = None,
    allow_non_loopback: Annotated[
        bool,
        typer.Option(
            "--allow-non-loopback",
            help="Permit a non-loopback bind (unsupported for shared use).",
        ),
    ] = False,
) -> None:
    """Start the local API/UI daemon (127.0.0.1:7611 by default)."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc

    resolved_host = host or settings.host
    if resolved_host not in _LOOPBACK_HOSTS:
        if not allow_non_loopback:
            emit_error(
                "CONFIG_INVALID",
                f"refusing non-loopback --host {resolved_host}; the daemon is "
                "loopback-only. Pass --allow-non-loopback to override (unsupported "
                "for shared use).",
                json_output=False,
            )
            raise typer.Exit(code=5)
        typer.echo(
            f"warning: binding to non-loopback host {resolved_host}; this deployment "
            "is unsupported and exposes the local API beyond 127.0.0.1",
            err=True,
        )

    import uvicorn

    uvicorn.run(
        "oft.api.app:create_app",
        factory=True,
        host=resolved_host,
        port=port or settings.port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    app()
