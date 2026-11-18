"""``oft doctor``: preflight diagnostics that work with the daemon down."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ERROR_CATALOG
from oft.application.health import build_doctor
from oft.bootstrap import build_container
from oft.cli.output import render_doctor_table
from oft.config import ConfigError, load_settings

_EXIT_CODES = {
    "CONFIG_INVALID": 5,
    "AUTH_REQUIRED": 5,
    "AUTH_INVALID": 5,
    "NOT_FOUND": 1,
    "DATA_UNAVAILABLE": 1,
    "DAEMON_NOT_RUNNING": 1,
}


def exit_code_for(code: str) -> int:
    return _EXIT_CODES.get(code, 1)


def emit_error(
    code: str,
    message: str,
    *,
    json_output: bool,
    details: list[dict[str, Any]] | None = None,
) -> None:
    """Print a catalog error; JSON mode keeps the standard envelope."""
    if json_output:
        from oft.api.envelope import error_envelope

        spec = ERROR_CATALOG.get(code, ERROR_CATALOG["INTERNAL_ERROR"])
        request_id = new_request_id()
        typer.echo(
            json.dumps(
                error_envelope(
                    code=code if code in ERROR_CATALOG else "INTERNAL_ERROR",
                    message=message,
                    http_status=spec.http_status,
                    category=spec.category,
                    retryable=spec.retryable,
                    reference_id=request_id,
                    request_id=request_id,
                    details=details,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(f"error: {message}", err=True)


def doctor_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the doctor report as JSON."),
    ] = False,
) -> None:
    """Check config, storage, market data, clock, and daemon reachability."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc

    container = build_container(settings)
    report = build_doctor(
        container.settings,
        container.store,
        container.market_data,
        container.clock,
    )
    if json_output:
        typer.echo(
            json.dumps(
                success_envelope(
                    asdict(report),
                    request_id=new_request_id(),
                    generated_at=report.checked_at,
                    provenance="CALC",
                    data_quality="COMPLETE" if report.status == "READY" else "PARTIAL",
                ),
                indent=2,
            )
        )
    else:
        rendered = render_doctor_table(report)
        telegram = report.telegram
        if telegram:
            chats = ", ".join(
                f"{entry['chat_id']}: {entry['failed']} failed"
                for entry in telegram.get("per_chat", [])
            )
            rendered += (
                f"\nTelegram  state {telegram['poller_state']} · "
                f"last poll {telegram.get('last_poll_at') or 'never'} · "
                f"last send {telegram.get('last_send_at') or 'never'} · "
                f"{telegram.get('failing_receipts', 0)} failing receipt(s)"
                f"{f' · {chats}' if chats else ''}"
            )
        typer.echo(rendered)
    # Diagnostics are informational: OFFLINE/DEGRADED still exit 0 (ADR-0003).
    raise typer.Exit(code=0)
