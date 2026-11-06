"""``oft backfill run`` and ``oft backfill status``."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, NoReturn

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error
from oft.config import ConfigError, load_settings

backfill_app = typer.Typer(
    help="Historical archive backfill (data.binance.vision).",
    no_args_is_help=True,
    add_completion=False,
)

_EXIT_SUCCESS = 0
_EXIT_PARTIAL = 3
_EXIT_BLOCKED = 4
_EXIT_CONFIG = 5

_CONFIG_CODES = frozenset({"CONFIG_INVALID", "AUTH_REQUIRED", "AUTH_INVALID"})
_BLOCKED_CODES = frozenset(
    {
        "BACKFILL_ALREADY_RUNNING",
        "BACKFILL_CHECKSUM_MISMATCH",
        "VALIDATION_FAILED",
        "DATA_UNAVAILABLE",
        "UNSAFE_ARCHIVE",
        "DAEMON_NOT_RUNNING",
    }
)


def exit_code_for_status(status: str) -> int:
    """Map a run status to the documented CLI exit codes."""
    normalized = status.upper()
    if normalized == "COMPLETE":
        return _EXIT_SUCCESS
    if normalized == "PARTIAL":
        return _EXIT_PARTIAL
    if normalized in {"RUNNING", "BLOCKED", "FAILED"}:
        return _EXIT_BLOCKED
    return _EXIT_BLOCKED


def exit_code_for_error(code: str) -> int:
    if code in _CONFIG_CODES:
        return _EXIT_CONFIG
    if code in _BLOCKED_CODES:
        return _EXIT_BLOCKED
    return _EXIT_BLOCKED


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=_EXIT_CONFIG) from exc


def _emit_error_and_exit(code: str, message: str, json_output: bool) -> NoReturn:
    emit_error(code, message, json_output=json_output)
    raise typer.Exit(code=exit_code_for_error(code))


def _run(data: dict[str, Any]) -> dict[str, Any] | None:
    value = data.get("run")
    return value if isinstance(value, dict) else None


def _render_estimate(data: dict[str, Any]) -> str:
    estimate = data.get("estimate") or {}
    rows = (
        ("Symbol", str(estimate.get("symbol", "-"))),
        ("Data type", str(estimate.get("data_type", "-"))),
        ("Range", f"{estimate.get('start', '-')} → {estimate.get('end', '-')}"),
        ("Days", str(estimate.get("days", "-"))),
        ("Files", str(estimate.get("files", "-"))),
        ("Estimated bytes", str(estimate.get("estimated_bytes", "-"))),
        ("Estimated MB", str(estimate.get("estimated_mb", "-"))),
    )
    width = max(len(label) for label, _ in rows)
    lines = ["Backfill estimate", ""]
    lines.extend(f"{label:<{width}}  {value}" for label, value in rows)
    lines.append("")
    lines.append("No data was downloaded. Re-run with --yes to start the backfill.")
    return "\n".join(lines)


def _render_run(data: dict[str, Any]) -> str:
    run = _run(data) or {}
    files = data.get("files") or []
    lines = [
        "Backfill run",
        "",
        f"Run ID            {run.get('run_id', '-')}",
        f"Symbol            {run.get('symbol', '-')}",
        f"Range             {run.get('start_date', '-')} → {run.get('end_date', '-')}",
        f"Status            {run.get('status', '-')}",
        (
            "Files verified    "
            f"{run.get('files_verified', 0)}/{run.get('files_total', 0)}"
            f" · failed {run.get('files_failed', 0)}"
        ),
        f"Processed bytes   {run.get('processed_bytes', 0)}",
    ]
    if run.get("error_code"):
        lines.append(f"Error             {run['error_code']}: {run.get('message') or ''}")
    if files:
        lines.append("")
        lines.append(f"{'FILE':<14}{'STATUS':<12}{'BYTES':>12}{'ROWS':>8}  VERIFIED")
        for row in files:
            lines.append(
                f"{str(row.get('date', '-')):<14}"
                f"{str(row.get('status', '-')):<12}"
                f"{row.get('bytes', 0):>12}"
                f"{row.get('rows', 0):>8}  "
                f"{row.get('verified_at') or '-'}"
            )
    return "\n".join(lines)


def _render_status(data: dict[str, Any]) -> str:
    return _render_run(data)


@backfill_app.command("run")
def run(
    symbol: Annotated[str, typer.Option("--symbol", help="Contract symbol, e.g. BTCUSDT.")],
    start: Annotated[str, typer.Option("--start", help="First day (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--end", help="Last day (YYYY-MM-DD).")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the download; without it only an estimate is shown."),
    ] = False,
    data_type: Annotated[
        str, typer.Option("--data-type", help="Archive data type directory.")
    ] = "aggTrades",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
) -> None:
    """Estimate or run a resumable one-week backfill through the daemon."""
    settings = _settings_or_exit(json_output)
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        _emit_error_and_exit(
            "VALIDATION_FAILED", "--start and --end must be YYYY-MM-DD dates.", json_output
        )
    payload = {
        "symbol": symbol,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "data_type": data_type,
        "confirm": yes,
    }
    try:
        envelope = ApiClient.from_settings(settings).create_backfill(payload)
    except ApiClientError as exc:
        _emit_error_and_exit(exc.code, exc.message, json_output)

    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        _emit_error_and_exit("INTERNAL_ERROR", "API returned no backfill data.", json_output)

    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(_render_run(data) if yes else _render_estimate(data))

    if not yes:
        raise typer.Exit(code=_EXIT_SUCCESS)
    run_data = _run(data)
    if run_data is None:
        _emit_error_and_exit("INTERNAL_ERROR", "API returned no backfill run.", json_output)
    raise typer.Exit(code=exit_code_for_status(str(run_data.get("status", ""))))


@backfill_app.command("status")
def status(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID; omit to read the latest run."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
) -> None:
    """Show bytes, rows, verified count, and status per archive file."""
    settings = _settings_or_exit(json_output)
    client = ApiClient.from_settings(settings)
    try:
        envelope = client.get_backfill(run_id) if run_id else client.get_latest_backfill()
    except ApiClientError as exc:
        _emit_error_and_exit(exc.code, exc.message, json_output)

    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        _emit_error_and_exit("NOT_FOUND", "No backfill run found.", json_output)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(_render_status(data))
    run_data = _run(data)
    if run_data is None:
        _emit_error_and_exit("INTERNAL_ERROR", "API returned no backfill run.", json_output)
    raise typer.Exit(code=exit_code_for_status(str(run_data.get("status", ""))))
