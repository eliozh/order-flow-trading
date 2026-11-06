"""``oft open``: print (and optionally open) the one-time browser bootstrap URL."""

from __future__ import annotations

import webbrowser
from typing import Annotated

import typer

from oft.api.auth import _origin, read_browser_bootstrap
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, load_settings


def open_command(
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Print the URL without launching a browser."),
    ] = False,
) -> None:
    """Print the one-time browser authentication URL for the running daemon."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc

    secret = read_browser_bootstrap(settings)
    if secret is None:
        emit_error(
            "DAEMON_NOT_RUNNING",
            "No browser bootstrap secret found. Start the daemon with `oft serve` first.",
            json_output=False,
        )
        raise typer.Exit(code=1)

    url = f"{_origin(settings.host, settings.port)}/?bootstrap={secret}"
    typer.echo(url)
    if not no_open:
        webbrowser.open(url)
