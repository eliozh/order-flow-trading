"""``oft watchlist``: list/search/groups plus star/unstar mutation commands.

``list``, ``search``, and ``groups`` read the SQLite symbol registry directly.
``star``/``unstar`` change only the local star flag through the API and are
aliased by ``add``/``remove``; no exchange call, metadata edit, or scan trigger
is involved.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from oft.api.envelope import new_request_id, success_envelope
from oft.api.errors import ERROR_CATALOG
from oft.application.symbol_registry import SymbolRegistry
from oft.bootstrap import build_container
from oft.cli.api_client import ApiClient, ApiClientError
from oft.cli.commands.doctor import emit_error, exit_code_for
from oft.config import ConfigError, load_settings
from oft.domain.symbols import SymbolContract
from oft.ports.store import StoreUnavailableError

watchlist_app = typer.Typer(
    help="Symbol registry and watchlist star commands.",
    no_args_is_help=True,
    add_completion=False,
)

_FORMATS = frozenset({"table", "json"})
_GROUPS = frozenset({"crypto", "tradfi"})
_EMPTY_MESSAGE = "No symbols in the registry. Run the daemon to ingest exchangeInfo."


def _resolve_output(format_: str, json_output: bool) -> bool:
    if format_ not in _FORMATS:
        emit_error(
            "VALIDATION_FAILED",
            f"unsupported format {format_!r}; use table or json.",
            json_output=json_output,
        )
        raise typer.Exit(code=2)
    return json_output or format_ == "json"


def _open_registry(json_output: bool) -> SymbolRegistry:
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc
    # Read-only path: never initialize or write the database.
    return build_container(settings).symbol_registry


def _emit(
    items: tuple[SymbolContract, ...],
    payload: dict[str, Any],
    table: str,
    json_output: bool,
) -> None:
    if json_output:
        data = {**payload, "items": [item.model_dump(mode="json") for item in items]}
        typer.echo(
            json.dumps(
                success_envelope(
                    data,
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE" if items else "MISSING",
                ),
                indent=2,
            )
        )
    else:
        typer.echo(table)


def render_symbols_table(contracts: tuple[SymbolContract, ...], *, title: str) -> str:
    """Render a deterministic, copy-friendly grouped registry table."""
    if not contracts:
        return _EMPTY_MESSAGE
    lines = [title, ""]
    header = (
        f"{'SYMBOL':<16}{'TYPE':<20}{'TICK':<10}{'STEP':<10}"
        f"{'MIN QTY':<12}{'MIN NOTIONAL':<14}{'STATUS':<10}LISTED"
    )
    for tradifi, label in ((False, "CRYPTO"), (True, "TRADFI")):
        group = [contract for contract in contracts if contract.tradifi is tradifi]
        if not group:
            continue
        lines.append(f"{label} ({len(group)})")
        lines.append(header)
        for contract in group:
            lines.append(
                f"{contract.symbol:<16}{contract.contract_type:<20}"
                f"{str(contract.tick_size):<10}{str(contract.step_size):<10}"
                f"{str(contract.min_qty):<12}{str(contract.min_notional):<14}"
                f"{contract.status:<10}{contract.listed_date}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_groups(counts: dict[str, int]) -> str:
    rows = (
        ("CRYPTO", counts["crypto"]),
        ("TRADFI", counts["tradfi"]),
        ("TOTAL", counts["total"]),
    )
    width = max(len(label) for label, _ in rows)
    return "\n".join(["SYMBOL GROUPS", *[f"{label:<{width}}  {value}" for label, value in rows]])


@watchlist_app.command("list")
def list_command(
    format_: Annotated[
        str, typer.Option("--format", help="Output format: table or json.")
    ] = "table",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
    group: Annotated[
        str | None, typer.Option("--group", help="Filter to crypto or tradfi.")
    ] = None,
    starred: Annotated[bool, typer.Option("--starred", help="Only starred symbols.")] = False,
) -> None:
    """List registry contracts grouped by Crypto / TradFi."""
    as_json = _resolve_output(format_, json_output)
    if group is not None and group not in _GROUPS:
        emit_error(
            "VALIDATION_FAILED",
            f"unsupported group {group!r}; use crypto or tradfi.",
            json_output=as_json,
        )
        raise typer.Exit(code=2)
    registry = _open_registry(as_json)
    tradifi = None if group is None else group == "tradfi"
    try:
        contracts = registry.list_symbols(tradifi=tradifi, starred=True if starred else None)
    except StoreUnavailableError:
        emit_error(
            "DATA_UNAVAILABLE",
            ERROR_CATALOG["DATA_UNAVAILABLE"].message,
            json_output=as_json,
        )
        raise typer.Exit(code=1) from None
    _emit(
        contracts,
        {"total": len(contracts), "group": group, "starred": starred},
        render_symbols_table(contracts, title="WATCHLIST · symbol registry"),
        as_json,
    )


@watchlist_app.command("search")
def search_command(
    query: str,
    format_: Annotated[
        str, typer.Option("--format", help="Output format: table or json.")
    ] = "table",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
) -> None:
    """Search the registry by exact symbol or substring."""
    as_json = _resolve_output(format_, json_output)
    registry = _open_registry(as_json)
    try:
        contracts = registry.list_symbols(query=query)
    except StoreUnavailableError:
        emit_error(
            "DATA_UNAVAILABLE",
            ERROR_CATALOG["DATA_UNAVAILABLE"].message,
            json_output=as_json,
        )
        raise typer.Exit(code=1) from None
    _emit(
        contracts,
        {"total": len(contracts), "query": query},
        render_symbols_table(contracts, title=f"SEARCH · {query}"),
        as_json,
    )


@watchlist_app.command("groups")
def groups_command(
    format_: Annotated[
        str, typer.Option("--format", help="Output format: table or json.")
    ] = "table",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the API envelope as JSON.")
    ] = False,
) -> None:
    """Report Crypto vs TradFi counts."""
    as_json = _resolve_output(format_, json_output)
    registry = _open_registry(as_json)
    try:
        counts = registry.group_counts()
    except StoreUnavailableError:
        emit_error(
            "DATA_UNAVAILABLE",
            ERROR_CATALOG["DATA_UNAVAILABLE"].message,
            json_output=as_json,
        )
        raise typer.Exit(code=1) from None
    if as_json:
        typer.echo(
            json.dumps(
                success_envelope(
                    counts,
                    request_id=new_request_id(),
                    provenance="CALC",
                    data_quality="COMPLETE" if counts["total"] else "MISSING",
                ),
                indent=2,
            )
        )
    else:
        typer.echo(_render_groups(counts))


def _set_star(symbol: str, starred: bool, json_output: bool) -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=json_output)
        raise typer.Exit(code=5) from exc
    try:
        envelope = ApiClient.from_settings(settings).set_starred(symbol, starred)
    except ApiClientError as exc:
        emit_error(exc.code, exc.message, json_output=json_output)
        raise typer.Exit(code=exit_code_for(exc.code)) from exc
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        emit_error("INTERNAL_ERROR", "API returned no star data.", json_output=json_output)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(envelope, indent=2))
    else:
        typer.echo(f"{'starred' if starred else 'unstarred'} {symbol.strip().upper()}")


@watchlist_app.command("star")
def star_command(
    symbol: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Star a registry symbol."""
    _set_star(symbol, True, json_output)


@watchlist_app.command("unstar")
def unstar_command(
    symbol: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Unstar a registry symbol."""
    _set_star(symbol, False, json_output)


@watchlist_app.command("add")
def add_command(
    symbol: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Alias for ``star``: add a symbol to the next scan."""
    _set_star(symbol, True, json_output)


@watchlist_app.command("remove")
def remove_command(
    symbol: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Alias for ``unstar``: remove a symbol from the next scan."""
    _set_star(symbol, False, json_output)
