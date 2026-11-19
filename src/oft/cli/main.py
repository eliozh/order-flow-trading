"""The ``oft`` command tree."""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from oft.cli.commands.backfill import backfill_app
from oft.cli.commands.budget import budget_app
from oft.cli.commands.config import config_app
from oft.cli.commands.daemon import daemon_app
from oft.cli.commands.doctor import doctor_command, emit_error
from oft.cli.commands.init import init_command
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
from oft.cli.commands.system import system_app
from oft.cli.commands.telegram import telegram_app
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
app.command(name="init")(init_command)
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
app.add_typer(system_app, name="system")
app.add_typer(telegram_app, name="telegram")

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


def _uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's own stderr loggers, isolated from the root (daemon.log) handlers.

    ``configure_logging`` installs root handlers that write JSON to stdout and
    ``daemon.log``. uvicorn emits plaintext diagnostics; letting them propagate
    would append non-JSON lines to ``daemon.log`` and make a filtered
    ``system logs`` report dropped/PARTIAL data. ``propagate: False`` on every
    uvicorn logger keeps them on stderr only.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": False,
            },
            # uvicorn 0.54 indexes this key whenever ``use_colors`` is a bool,
            # even with access logging disabled.
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


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
        # uvicorn installs its own stderr handlers with propagation disabled, so
        # its plaintext records never reach the root handlers that write
        # daemon.log (which must stay JSON-only for ``system logs``).
        log_config=_uvicorn_log_config(),
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


def _build_freshness_guard(container: Any) -> Any:
    """Build the daemon's freshness guard with the Telegram degradation sink.

    The sink is only wired when the container has a notification service;
    ``FreshnessGuard`` itself still logs every transition, so a missing push
    target never hides the structured-log alert.
    """
    from oft.application.freshness import FreshnessGuard
    from oft.application.notifications import NotificationFreshnessSink

    notifications = getattr(container, "notifications", None)
    alert_sink = NotificationFreshnessSink(notifications) if notifications is not None else None
    return FreshnessGuard(clock=container.clock, alert_sink=alert_sink)


# A live receive can block for up to ~30 s; stop() closes the socket stream
# first, then joins only long enough for the supervisor's finally to drain and
# checkpoint the buffer. 15 s covers one in-flight connect/subscribe handshake
# in addition to a bounded reconnect backoff.
LIVE_INGESTION_JOIN_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class LiveIngestionHandle:
    """Owns the live-ingestion worker so shutdown can flush before process exit."""

    stop_event: threading.Event
    thread: threading.Thread
    supervisor: Any
    clients: tuple[Any, ...] = ()

    def stop(self, *, timeout: float = LIVE_INGESTION_JOIN_TIMEOUT_SECONDS) -> bool:
        """Signal, unblock, join; ``True`` when the worker actually exited."""
        self.stop_event.set()
        for client in self.clients:
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _run_live_ingestion(
    settings: Settings,
    replay_file: Path | None,
    symbols: str | None,
    *,
    container: Any | None = None,
) -> LiveIngestionHandle | None:
    """Run one ordered replay, or start the continuous live WS ingester.

    The live path returns a handle whose ``stop`` flushes the supervisor's
    buffer; the replay, idle, empty-symbol, and test-environment paths are
    synchronous or intentionally inert and return ``None``.
    """
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
            return None
        if settings.env.value == "test":
            # Never open a socket from a test environment.
            WsStateStore(state_path, container.clock).write("DISCONNECTED", {}, {})
            return None
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
        # Created before the supervisor so reconnect backoff is interruptible:
        # ``stop`` sets the event and the sleeper returns immediately instead of
        # blocking for up to the backoff maximum while the join times out.
        stop_event = threading.Event()
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
            freshness=_build_freshness_guard(container),
            sleep=stop_event.wait,
        )

        def _run_ws_thread() -> None:
            try:
                supervisor.run_forever(stop=stop_event.is_set)
            except Exception as exc:  # noqa: BLE001 - thread must fail visibly
                import structlog

                structlog.get_logger(__name__).error(
                    "ws_ingest_thread_failed", error=type(exc).__name__
                )
                WsStateStore(state_path, container.clock).write("FAILED", {}, {})

        worker = threading.Thread(
            target=_run_ws_thread,
            name="oft-ws-ingest",
            daemon=True,
        )
        worker.start()
        return LiveIngestionHandle(
            stop_event=stop_event,
            thread=worker,
            supervisor=supervisor,
            clients=(live_client,),
        )
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
    return None


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


if __name__ == "__main__":
    # The detached-restart fallback launches ``python -m oft.cli.main daemon start``.
    main()
