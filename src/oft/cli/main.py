"""The ``oft`` command tree."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from oft.cli.commands.backfill import backfill_app
from oft.cli.commands.budget import budget_app
from oft.cli.commands.config import config_app
from oft.cli.commands.daemon import daemon_app
from oft.cli.commands.doctor import doctor_command, emit_error
from oft.cli.commands.kill import kill_app
from oft.cli.commands.knowledge import knowledge_app
from oft.cli.commands.llm import llm_app
from oft.cli.commands.log import log_app
from oft.cli.commands.open import open_command
from oft.cli.commands.opp import opp_app
from oft.cli.commands.paper import paper_app
from oft.cli.commands.pos import pos_app
from oft.cli.commands.prompt import prompt_app
from oft.cli.commands.replay import replay_app
from oft.cli.commands.report import report_app
from oft.cli.commands.risk import risk_app
from oft.cli.commands.scan import scan_app
from oft.cli.commands.status import status_command
from oft.cli.commands.trigger import trigger_app
from oft.cli.commands.watchlist import watchlist_app
from oft.config import ConfigError, Settings, load_settings

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
app.command(name="open")(open_command)
app.add_typer(scan_app, name="scan")
app.add_typer(report_app, name="report")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(backfill_app, name="backfill")
app.add_typer(daemon_app, name="daemon")
app.add_typer(opp_app, name="opp")
app.add_typer(trigger_app, name="trigger")
app.add_typer(paper_app, name="paper")
app.add_typer(pos_app, name="pos")
app.add_typer(log_app, name="log")
app.add_typer(prompt_app, name="prompt")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(kill_app, name="kill")
app.add_typer(llm_app, name="llm")
app.add_typer(budget_app, name="budget")
app.add_typer(risk_app, name="risk")
app.add_typer(config_app, name="config")
app.add_typer(replay_app, name="replay")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _resolve_serve_settings(
    host: str | None,
    port: int | None,
    allow_non_loopback: bool,
) -> Settings:
    try:
        base = load_settings()
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc

    resolved_host = host or base.host
    resolved_port = port or base.port
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

    # CLI overrides must reach the app factory: create_app() must not reload the
    # environment and silently fall back to the default host/port.
    try:
        return load_settings(host=resolved_host, port=resolved_port)
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", exc.message, json_output=False)
        raise typer.Exit(code=5) from exc


def _run_uvicorn(settings: Settings) -> None:
    def app_factory() -> Any:
        from oft.api.app import create_app

        return create_app(settings)

    import uvicorn

    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


def _active_symbols(explicit: str | None, starred: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve the ingestion universe: explicit ``--symbols`` overrides stars.

    Star changes take effect on the next connect/reconnect; the live
    connection's stream set is fixed when the supervisor is constructed.
    """
    if explicit is not None:
        return tuple(item.strip().upper() for item in explicit.split(",") if item.strip())
    return tuple(symbol.upper() for symbol in starred)


def _build_limited_clients(container: Any) -> tuple[Any, Any]:
    """Build the live clients sharing one rate bucket and WS stream budget."""
    from oft.adapters.binance_rest import BinanceRestClient
    from oft.adapters.binance_ws import BinanceWsClient

    return (
        BinanceWsClient(subscription_budget=container.ws_budget),
        BinanceRestClient(rate_limiter=container.rate_limiter),
    )


def _run_live_ingestion(
    settings: Settings,
    replay_file: Path | None,
    symbols: str | None,
    *,
    container: Any | None = None,
) -> None:
    """Run one ordered replay, or start the continuous live WS ingester."""
    import threading

    from oft.adapters.parquet_store import (
        FlushPolicy,
        LiveParquetBuffer,
        quarantine_orphan_live_parts,
    )
    from oft.application.ingestion_supervisor import IngestionSupervisor, WsStateStore
    from oft.bootstrap import build_container

    resolved_container = container or build_container(settings, initialize=True)
    container = resolved_container
    # A crash between rename and manifest insert leaves an unregistered part;
    # move it aside on startup instead of silently adopting it.
    quarantine_orphan_live_parts(settings, container.store, clock=container.clock)
    state_path = settings.data_dir / "run" / "ws_state.json"
    starred = tuple(contract.symbol for contract in container.symbol_registry.list_starred())
    symbol_list = _active_symbols(symbols, starred)
    if replay_file is None:
        if not symbol_list:
            # No explicit override and nothing starred: start idle, never default.
            WsStateStore(state_path, container.clock).write("IDLE", {}, {})
            return
        if settings.env.value == "test":
            # Never open a socket from a test environment.
            WsStateStore(state_path, container.clock).write("DISCONNECTED", {}, {})
            return
        live_client, recovery_client = _build_limited_clients(container)
        live_parquet = LiveParquetBuffer(
            settings=settings,
            store=container.store,
            clock=container.clock,
            policy=FlushPolicy(),
        )
        symbol_provider = None
        if symbols is None:

            def _starred_provider() -> tuple[str, ...]:
                return tuple(
                    contract.symbol for contract in container.symbol_registry.list_starred()
                )

            symbol_provider = _starred_provider
        supervisor = IngestionSupervisor(
            settings=settings,
            source=(),
            store=container.store,
            parquet=live_parquet,
            clock=container.clock,
            client=live_client,
            recovery=recovery_client,
            symbols=symbol_list,
            symbol_provider=symbol_provider,
        )

        def _run_ws_thread() -> None:
            try:
                supervisor.run_forever()
            except Exception as exc:  # noqa: BLE001 - thread must fail visibly
                import structlog

                structlog.get_logger(__name__).error(
                    "ws_ingest_thread_failed", error=type(exc).__name__
                )
                WsStateStore(state_path, container.clock).write("FAILED", {}, {})

        threading.Thread(
            target=_run_ws_thread,
            name="oft-ws-ingest",
            daemon=True,
        ).start()
        return
    buffer = LiveParquetBuffer(
        settings=settings,
        store=container.store,
        clock=container.clock,
        policy=FlushPolicy(),
    )
    supervisor = IngestionSupervisor(
        settings=settings,
        source=replay_file.read_text(encoding="utf-8").splitlines(),
        store=container.store,
        parquet=buffer,
        clock=container.clock,
        symbols=symbol_list,
    )
    supervisor.run()


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
    settings = _resolve_serve_settings(host, port, allow_non_loopback)
    _run_uvicorn(settings)


def main() -> None:
    app()
