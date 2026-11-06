"""``oft scan run`` and ``oft scan status``."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.cli.output import render_scan_table
from oft.config import ConfigError, load_settings
from oft.ports.store import ScanRecord

scan_app = typer.Typer(
    help="Scan commands (fixture-backed in the walking skeleton).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit_scan(record: ScanRecord, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    asdict(record),
                    request_id=new_request_id(),
                    generated_at=record.finished_at,
                    provenance="CALC",
                    data_quality=record.data_quality,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(render_scan_table(record))


@scan_app.command("run")
def run(
    fixture: Annotated[Path, typer.Option("--fixture", help="aggTrades CSV fixture.")],
    symbol: Annotated[str, typer.Option("--symbol", help="Contract symbol.")] = "BTCUSDT",
    period: Annotated[str, typer.Option("--period", help="Metric period, e.g. 4h.")] = "4h",
    metrics_only: Annotated[
        bool,
        typer.Option("--metrics-only/--no-metrics-only", help="CALC-only scan."),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Run one fixture-backed scan through the daemon (the only writer)."""
    settings = _settings_or_exit(json_output)
    payload = {
        "fixture_path": str(fixture),
        "symbol": symbol,
        "period": period,
        "metrics_only": metrics_only,
    }

    try:
        envelope = ApiClient.from_settings(settings).create_scan(payload)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no scan data.", json_output=json_output)
        raise typer.Exit(code=1)
    _emit_scan(ScanRecord(**data), json_output)


@scan_app.command("status")
def scan_status(
    scan_id: str,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the API envelope as JSON."),
    ] = False,
) -> None:
    """Read one scan through the daemon API."""
    settings = _settings_or_exit(json_output)

    try:
        envelope = ApiClient.from_settings(settings).get_scan(scan_id)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc

    data = envelope.get("data")
    if not isinstance(data, dict):
        emit_error("NOT_FOUND", f"Scan {scan_id} does not exist.", json_output=json_output)
        raise typer.Exit(code=1)
    _emit_scan(ScanRecord(**data), json_output)
