"""``oft paper balance|orders|fills|equity``: the simulated account (read-only)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings

paper_app = typer.Typer(
    help="Paper account read models (no live broker, no reset in this slice).",
    no_args_is_help=True,
    add_completion=False,
)


def _settings_or_exit(json_output: bool) -> Any:
    try:
        return load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc


def _emit(settings: Any, method: str, json_output: bool, *, limit: int | None = None) -> None:
    client = ApiClient.from_settings(settings)
    try:
        if method == "balance":
            envelope = client.paper_balance()
        elif method == "orders":
            envelope = client.paper_orders(limit or 100)
        elif method == "fills":
            envelope = client.paper_fills(limit or 100)
        else:
            envelope = client.paper_equity()
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
        return
    data = envelope.get("data") or {}
    if method in {"balance", "equity"}:
        typer.echo(
            f"{data.get('account_id', '-')} · {data.get('currency', 'USDT')}"
            f" · balance {data.get('balance', '-')}"
        )
        if method == "equity":
            typer.echo(
                f"equity {data.get('equity', '-')} · open positions {data.get('open_positions', 0)}"
            )
            typer.echo(
                f"realized {data.get('realized_pnl_usd', '-')}"
                f" · fees {data.get('fees_usd', '-')}"
                f" · closed trades {data.get('closed_trades', 0)}"
            )
        stale = data.get("stale_inputs") or []
        if stale:
            # Equity fell back to cash (for example a missing mark); say so
            # instead of presenting a degraded number as complete (S7).
            typer.echo("  stale: " + ", ".join(str(name) for name in stale))
        return
    items = data.get("items") or []
    if not items:
        typer.echo(f"No paper {method}.")
        return
    for item in items:
        if method == "orders":
            typer.echo(
                f"{item.get('order_id', '-')} · {item.get('symbol', '-')}"
                f" · {item.get('side', '-')} {item.get('type', '-')}"
                f" · {item.get('status', '-')} · qty {item.get('quantity', '-')}"
            )
        else:
            typer.echo(
                f"{item.get('fill_id', '-')} · {item.get('symbol', '-')}"
                f" · {item.get('side', '-')} @ {item.get('price', '-')}"
                f" · qty {item.get('quantity', '-')} · fee {item.get('fee', '-')}"
            )


@paper_app.command("balance")
def paper_balance(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the paper account balance."""
    _emit(_settings_or_exit(json_output), "balance", json_output)


@paper_app.command("orders")
def paper_orders(
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List paper orders."""
    _emit(_settings_or_exit(json_output), "orders", json_output, limit=limit)


@paper_app.command("fills")
def paper_fills(
    limit: Annotated[int, typer.Option("--limit", help="Rows to show.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List paper fills."""
    _emit(_settings_or_exit(json_output), "fills", json_output, limit=limit)


@paper_app.command("equity")
def paper_equity(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show paper equity (balance; PnL/funding are out of scope)."""
    _emit(_settings_or_exit(json_output), "equity", json_output)
